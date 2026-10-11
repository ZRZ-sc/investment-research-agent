"""PostgreSQL 连接池（asyncpg）：任务状态 / 中间产出 / 最终报告（需求 3.8）。

用法：
    await init_pool()          # 进程/线程启动时
    pool = get_pool()          # 同步取池，随后 async with pool.acquire() as conn
    await close_pool()         # 用完释放

连接串由 app.config.settings.pg_dsn 提供（PG_HOST/PG_PORT/PG_USER/PG_PASSWORD/PG_DATABASE）；
本机开发：PostgreSQL 容器宿主端口为 5433（见 .env.local），容器内仍是 5432。

⚠️ 并发要点（实测踩坑）：池必须**按线程隔离**。
    Celery 用线程池并发执行任务（`-P threads -c 3`），每个任务在自己的线程里
    `asyncio.run(...)` 创建独立事件循环；而 asyncpg 的连接池**绑定创建它的事件循环**。
    早前用全局单例池时，任务 A 收尾 `close_pool()` 会把正在运行的 B/C 的池一起关掉，
    表现为 `InterfaceError: pool is closed` / `RuntimeError: 连接池尚未初始化`
    （N1 评测中 3 并发下 2/10 任务失败）。改为 threading.local 后每个并发任务各自持池。
"""
from __future__ import annotations

import threading

import asyncpg

from app.config import settings
from app.core.logging import logger

# 线程本地存储：API 进程（单事件循环主线程）与 worker 的每个任务线程各有一份
_local = threading.local()


async def init_pool() -> asyncpg.Pool:
    """建立**本线程**的连接池（幂等：已有则直接返回）。"""
    pool: asyncpg.Pool | None = getattr(_local, "pool", None)
    if pool is None:
        pool = await asyncpg.create_pool(
            dsn=settings.pg_dsn,
            min_size=settings.pg_pool_min,
            max_size=settings.pg_pool_max,
            command_timeout=30,
            server_settings={"application_name": settings.app_name},
        )
        _local.pool = pool
        logger.info(
            "postgres pool ready | {}:{}/{} (pool {}-{})",
            settings.pg_host,
            settings.pg_port,
            settings.pg_database,
            settings.pg_pool_min,
            settings.pg_pool_max,
        )
    return pool


async def close_pool() -> None:
    """关闭**本线程**的连接池（幂等）。"""
    pool: asyncpg.Pool | None = getattr(_local, "pool", None)
    if pool is not None:
        _local.pool = None
        await pool.close()
        logger.info("postgres pool closed")


def get_pool() -> asyncpg.Pool:
    """同步取本线程的连接池；未初始化时抛错（提示应先 init_pool）。"""
    pool: asyncpg.Pool | None = getattr(_local, "pool", None)
    if pool is None:
        raise RuntimeError("PostgreSQL 连接池尚未初始化：请先调用 app.db.postgres.init_pool()")
    return pool


async def ping() -> bool:
    """就绪探针：能否取到连接并执行查询。"""
    try:
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception as exc:  # 依赖故障不应让探针接口本身 500
        logger.warning("postgres ping failed: {}", exc)
        return False
