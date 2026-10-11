"""应用层限流：Redis 令牌桶（需求文档 3.5 错误码 429）。

设计要点：
- **令牌桶**而非固定窗口：允许合理突发（burst），长期速率受 per_minute 约束，
  且不会出现"窗口边界瞬间双倍流量"的问题。
- **原子性**：判定与扣减在 Redis 内用 Lua 一次完成，多实例部署下同样准确。
- **身份维度**：优先 user_id（业务主体）；缺失时退化为客户端 IP。
- **降级**：Redis 不可用时 fail-open（放行 + 告警）——需求文档 N8 要求依赖故障不得让服务整体不可用。
- **可选全局桶**：保护 CPU 密集型实例，超载时用 429 卸载而不是雪崩。
"""
import time

from fastapi import Request
from redis import asyncio as aioredis

from app.config import settings
from app.core.errors import RateLimitError
from app.core.logging import logger
from app.core.metrics import rate_limited_total
from app.db.redis import get_redis

# 令牌桶：返回 [是否放行, 剩余令牌]
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end

local elapsed = now - ts
if elapsed > 0 then
  tokens = math.min(capacity, tokens + elapsed * refill)
end

local allowed = 0
if tokens >= cost then
  allowed = 1
  tokens = tokens - cost
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return {allowed, math.floor(tokens)}
"""

_script = None


def _get_script(client: aioredis.Redis):
    global _script
    if _script is None:
        _script = client.register_script(_TOKEN_BUCKET_LUA)
    return _script


async def check_bucket(
    key: str, capacity: int, refill_per_sec: float, cost: int = 1
) -> tuple[bool, int]:
    """令牌桶判定，返回 (是否放行, 剩余令牌数)。Redis 异常时放行（降级）。"""
    if not settings.rate_limit_enabled:
        return True, capacity
    try:
        script = _get_script(get_redis())
        ttl = int(capacity / max(refill_per_sec, 0.001)) + 60
        result = await script(
            keys=[key], args=[capacity, refill_per_sec, cost, time.time(), ttl]
        )
        allowed, remaining = int(result[0]), int(result[1])
        return bool(allowed), remaining
    except Exception as exc:
        logger.warning("rate limit check failed (fail-open): {}", exc)
        return True, capacity


def identity_of(user_id: str | None, request: Request) -> str:
    """限流身份：优先 user_id，其次客户端 IP。"""
    if user_id:
        return f"user:{user_id}"
    client = request.client.host if request.client else "unknown"
    return f"ip:{client}"


async def enforce(scope: str, identity: str) -> int:
    """执行限流；超限抛 429（带 Retry-After）。返回剩余令牌数。"""
    if not settings.rate_limit_enabled:
        # 总开关关闭时直接放行（不触碰 Redis）
        return settings.rate_limit_burst

    capacity = max(1, settings.rate_limit_burst)
    refill = max(settings.rate_limit_per_minute, 1) / 60.0

    allowed, remaining = await check_bucket(
        f"ratelimit:{scope}:{identity}", capacity, refill
    )
    if not allowed:
        rate_limited_total.labels(scope=scope).inc()
        retry_after = max(1, int(capacity / refill))
        logger.warning(
            "rate limited | scope={} identity={} retry_after={}s", scope, identity, retry_after
        )
        raise RateLimitError(
            f"请求过于频繁，请在 {retry_after} 秒后重试",
            data={"scope": scope, "retry_after": retry_after},
            headers={"Retry-After": str(retry_after)},
        )

    # 全局保护桶（保护 CPU 密集型实例；默认关闭）
    if settings.rate_limit_global_per_minute > 0:
        g_capacity = max(
            1, settings.rate_limit_global_burst or settings.rate_limit_global_per_minute // 2
        )
        g_refill = settings.rate_limit_global_per_minute / 60.0
        g_allowed, _ = await check_bucket(f"ratelimit:global:{scope}", g_capacity, g_refill)
        if not g_allowed:
            rate_limited_total.labels(scope=f"{scope}:global").inc()
            logger.warning("rate limited (global) | scope={}", scope)
            raise RateLimitError(
                "服务当前负载较高，请稍后重试",
                data={"scope": f"{scope}:global"},
                headers={"Retry-After": "5"},
            )
    return remaining
