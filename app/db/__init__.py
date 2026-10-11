"""数据库客户端层：PostgreSQL（任务/中间产出/报告，需求 3.8）与 Redis（缓存/限流）。

- app.db.postgres：asyncpg 连接池
- app.db.redis：redis.asyncio 客户端（注意 get_redis() 为同步访问器，
  app/services/rate_limit.py 依赖该签名）
"""
