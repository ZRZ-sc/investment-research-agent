"""初始化数据库：建表（任务 / 中间产出 / 最终报告，需求文档 3.8）。

用法（两种等价方式）：
    .venv\\Scripts\\python.exe scripts\\init_db.py
    .venv\\Scripts\\python.exe -m scripts.init_db

连接参数取自 app/config.py（PG_HOST / PG_PORT / PG_USER / PG_PASSWORD / PG_DATABASE），
默认与 docker-compose.infra.yml 保持一致：research / researchpass / research @ localhost:5432。
全部语句均为 CREATE ... IF NOT EXISTS，**可重复执行（幂等）**。
"""
import asyncio
import sys
from pathlib import Path

import asyncpg

# 支持以脚本方式直接运行（python scripts/init_db.py）时也能 import app.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402  （必须在 sys.path 调整之后导入）

DDL = [
    # ---- 任务表（需求 3.8）----
    # progress / current_step / callback_url 三列文档 DDL 未列，但 3.10 的进度查询接口
    # （progress、current_step）与创建接口的 callback_url 需要落库，故一并建立。
    """CREATE TABLE IF NOT EXISTS research_tasks (
        task_id UUID PRIMARY KEY,
        company_name VARCHAR(100),
        ticker VARCHAR(10),
        status VARCHAR(20) NOT NULL DEFAULT 'queued',
        created_at TIMESTAMP DEFAULT NOW(),
        completed_at TIMESTAMP,
        error_message TEXT,
        retry_count INT DEFAULT 0,
        timeout_at TIMESTAMP,
        progress FLOAT DEFAULT 0,
        current_step VARCHAR(50),
        callback_url VARCHAR(500),
        CONSTRAINT ck_research_tasks_status CHECK (
            status IN ('pending','queued','running','completed','failed','need_human')
        )
    )""",
    # ---- 中间产出表（需求 F5：记录每一步产出，支持审计与人工干预）----
    """CREATE TABLE IF NOT EXISTS task_outputs (
        id SERIAL PRIMARY KEY,
        task_id UUID REFERENCES research_tasks(task_id) ON DELETE CASCADE,
        step VARCHAR(50),
        output_json JSONB,
        created_at TIMESTAMP DEFAULT NOW()
    )""",
    # ---- 最终报告表（需求 3.3 Task 3 / F4）----
    """CREATE TABLE IF NOT EXISTS research_reports (
        id SERIAL PRIMARY KEY,
        task_id UUID REFERENCES research_tasks(task_id) ON DELETE CASCADE,
        report_path VARCHAR(500),
        report_content TEXT,
        word_count INT,
        created_at TIMESTAMP DEFAULT NOW()
    )""",
    # ---- 索引 ----
    "CREATE INDEX IF NOT EXISTS idx_research_tasks_status ON research_tasks(status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_research_tasks_ticker ON research_tasks(ticker)",
    # 同一步骤重跑（需求 3.7 校验失败重跑）时按 (task_id, step) 覆盖，避免中间产出重复堆积
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_task_outputs_task_step ON task_outputs(task_id, step)",
    "CREATE INDEX IF NOT EXISTS idx_research_reports_task ON research_reports(task_id)",
    # ---- 状态取值口径（3.6 超时记为 failed；3.9 ticker 无法判定 → need_human）----
    """COMMENT ON COLUMN research_tasks.status IS
       'pending/queued/running/completed/failed/need_human；超时(>900s)按 3.6 记 failed 并在 error_message 说明'""",
]


async def main() -> None:
    conn = await asyncpg.connect(dsn=settings.pg_dsn)
    try:
        for stmt in DDL:
            await conn.execute(stmt)
        tables = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
        print(
            f"[ok] schema ready @ {settings.pg_host}:{settings.pg_port}/{settings.pg_database}"
        )
        print("[ok] tables:", ", ".join(r["table_name"] for r in tables))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
