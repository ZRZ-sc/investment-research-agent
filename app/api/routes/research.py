"""投研任务接口（需求文档 3.10）：创建 / 状态查询 / 报告获取。

| 接口 | 说明 | 关键约束 |
|------|------|---------|
| POST /api/v1/research | 创建任务：落库 + 入队 Celery | Bearer 鉴权（N6）+ 每 token 每分钟 1 个新任务（3.10）|
| GET  /api/v1/research/{task_id} | 状态、进度、中间产出、报告地址 | Bearer 鉴权 |
| GET  /api/v1/research/{task_id}/report | 最终报告正文 | Bearer 鉴权 |
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import verify_token
from app.config import settings
from app.core import metrics as m
from app.core.errors import DegradedError, NotFoundError, RateLimitError, ok
from app.core.logging import logger
from app.db.postgres import get_pool
from app.services.rate_limit import check_bucket
from app.worker.tasks import run_research_task

router = APIRouter(prefix="/api/v1/research", tags=["research"])

# task_outputs.step ←→ 3.10 响应里的 intermediate_outputs 键名
_STEP_TO_FIELD = {
    "profile": "company_profile",
    "financials": "financial_metrics",
    "report": "investment_report",
}


class ResearchRequest(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=100, description="公司名称，如 Apple Inc.")
    ticker: str | None = Field(None, max_length=10, description="可选；提供则跳过 TickerLookup")
    callback_url: str | None = Field(None, max_length=500, description="可选；任务完成后回调")


def _token_identity(request: Request) -> str:
    """按 token 限流的身份标识：只存哈希，不落明文 token。"""
    raw = request.headers.get("authorization", "")
    return "token:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


@router.post("", dependencies=[Depends(verify_token)])
async def create_research(payload: ResearchRequest, request: Request) -> dict:
    """创建投研任务：鉴权(N6) → 按 token 限流(3.10) → 落库 → 入队 Celery。"""
    identity = _token_identity(request)
    allowed, _ = await check_bucket(
        f"ratelimit:task_create:{identity}",
        capacity=settings.task_create_burst,
        refill_per_sec=settings.task_create_per_minute / 60.0,
    )
    if not allowed:
        retry_after = int(60 / max(settings.task_create_per_minute, 1))
        m.rate_limited_total.labels(scope="task_create").inc()
        raise RateLimitError(
            f"每个 token 每分钟最多触发 {settings.task_create_per_minute} 个新任务，请 {retry_after} 秒后重试",
            data={"scope": "task_create", "retry_after": retry_after},
            headers={"Retry-After": str(retry_after)},
        )

    task_id = uuid.uuid4()
    timeout_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        seconds=settings.task_timeout_seconds
    )
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO research_tasks
                 (task_id, company_name, ticker, status, progress, current_step, timeout_at, callback_url)
               VALUES ($1, $2, $3, 'queued', 0, '等待调度', $4, $5)""",
            task_id, payload.company_name, payload.ticker, timeout_at, payload.callback_url,
        )

    try:
        run_research_task.delay(str(task_id), payload.company_name, payload.ticker)
    except Exception as exc:
        # 队列不可用：不能让任务停在 queued（否则前端永远等不到），直接标失败并降级返回
        logger.error("enqueue failed | task_id={} err={}", task_id, exc)
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE research_tasks SET status='failed', error_message=$2, completed_at=NOW()
                    WHERE task_id=$1""",
                task_id, f"enqueue failed: {exc}"[:500],
            )
        m.research_tasks_total.labels(status="failed").inc()
        raise DegradedError("任务队列不可用，请稍后重试", data={"task_id": str(task_id)})

    logger.info(
        "task created | task_id={} company={} ticker={}", task_id, payload.company_name, payload.ticker
    )
    return ok(
        {
            "task_id": str(task_id),
            "status": "queued",
            "estimated_time": settings.task_estimated_seconds,
        },
        "Task created successfully",
    )


@router.get("/{task_id}", dependencies=[Depends(verify_token)])
async def get_research(task_id: UUID) -> dict:
    """任务状态 + 进度 + 中间产出（需求 3.10 GET 响应结构）。"""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM research_tasks WHERE task_id = $1", task_id)
        if row is None:
            raise NotFoundError(f"任务不存在：{task_id}")
        outputs = await conn.fetch(
            "SELECT step, output_json FROM task_outputs WHERE task_id = $1 ORDER BY id", task_id
        )
        report = await conn.fetchrow(
            """SELECT report_path, created_at FROM research_reports
                WHERE task_id = $1 ORDER BY id DESC LIMIT 1""",
            task_id,
        )

    intermediate: dict[str, dict | None] = {field: None for field in _STEP_TO_FIELD.values()}
    for item in outputs:
        field = _STEP_TO_FIELD.get(item["step"])
        if not field:
            continue
        raw = item["output_json"]
        intermediate[field] = json.loads(raw) if isinstance(raw, str) else raw

    return ok(
        {
            "task_id": str(row["task_id"]),
            "company_name": row["company_name"],
            "ticker": row["ticker"],
            "status": row["status"],
            "progress": round(row["progress"] or 0.0, 2),
            "current_step": row["current_step"],
            "intermediate_outputs": intermediate,
            "report_url": f"/api/v1/research/{row['task_id']}/report" if report else None,
            "error_message": row["error_message"],
            "retry_count": row["retry_count"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
        }
    )


@router.get("/{task_id}/report", dependencies=[Depends(verify_token)])
async def get_report(task_id: UUID) -> dict:
    """最终报告正文（需求 3.10：report_content / report_format / generated_at）。"""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT report_content, report_path, created_at FROM research_reports
                WHERE task_id = $1 ORDER BY id DESC LIMIT 1""",
            task_id,
        )
        if row is None:
            exists = await conn.fetchval("SELECT 1 FROM research_tasks WHERE task_id = $1", task_id)
            if not exists:
                raise NotFoundError(f"任务不存在：{task_id}")
            raise NotFoundError(
                "报告尚未生成：任务仍在执行或已失败", data={"task_id": str(task_id)}
            )

    return ok(
        {
            "report_content": row["report_content"],
            "report_format": settings.report_format,
            "report_path": row["report_path"],
            "generated_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
    )
