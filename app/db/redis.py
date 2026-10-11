"""Redis 客户端（限流令牌桶 + 通用缓存）。

分库约定（见 app/config.py 与本机 .env.local）：
    db0  REDIS_URL              限流 + 通用缓存
    db1  CELERY_BROKER_URL      Celery broker
    db2  CELERY_RESULT_BACKEND  Celery 结果后端

注意：get_redis() 是**同步**访问器（直接返回客户端对象），
app/services/rate_limit.py 依赖该签名（`_get_script(get_redis())`），请勿改成 async。
"""
from __future__ import annotations

from redis import asyncio as aioredis

from app.config import settings
from app.core.logging import logger

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """同步取（惰性创建）Redis 客户端；Redis 不可用时由调用方决定降级策略。"""
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
    return _client


async def init_redis() -> aioredis.Redis:
    """启动时建立连接并探活。"""
    client = get_redis()
    await client.ping()
    logger.info("redis ready | {}", settings.redis_url)
    return client


async def close_redis() -> None:
    """关闭连接（幂等）。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("redis closed")


async def ping() -> bool:
    """就绪探针：缓存/限流库（db0）是否可用。"""
    try:
        await get_redis().ping()
        return True
    except Exception as exc:
        logger.warning("redis ping failed: {}", exc)
        return False


async def ping_broker() -> bool:
    """就绪探针：Celery broker 库（db1）是否可用——worker 能否接单的关键依赖。"""
    client = aioredis.from_url(settings.celery_broker_url, socket_connect_timeout=3)
    try:
        await client.ping()
        return True
    except Exception as exc:
        logger.warning("celery broker ping failed: {}", exc)
        return False
    finally:
        await client.aclose()
