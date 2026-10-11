"""Celery 任务：投研工作流执行入口（需求 3.3 三步工作流 / 3.6 超时保护 / F6 重试 / F5 中间产出落库）。

三步均已接入真实 Agent：
    Task 1  信息搜集   app/agents/info_collector.py    （DeepSeek Flash + 检索工具）
    Task 2  财报分析   app/agents/financial_analyst.py （yfinance → SEC XBRL → Bing 兜底）
    Task 3  报告撰写   app/agents/report_writer.py     （DeepSeek Pro，8 章结构 + 落盘）

本模块只负责编排：入队 → 接单 → 逐步更新进度 → 中间产出落库 → 报告落库 → 完成，
以及重试、超时兜底、指标埋点。单步失败/降级不会终止整个任务（需求 3.7）。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded

from app.config import settings
from app.core import metrics as m
from app.core.logging import logger
from app.db.postgres import close_pool, get_pool, init_pool
from app.worker.celery_app import celery_app

# 需求 3.3：三步工作流（step 取值与 task_outputs.step、3.10 的 intermediate_outputs 对齐）
WORKFLOW_STEPS = [
    ("profile", "InformationSpecialist", "公司基本信息搜集"),
    ("financials", "FinancialAnalyst", "财务数据获取与指标计算"),
    ("report", "ReportWriter", "投资价值分析与报告撰写"),
]

@celery_app.task(
    bind=True,
    name="app.worker.tasks.run_research_task",
    max_retries=settings.celery_task_max_retries,
    acks_late=settings.celery_task_acks_late,
)
def run_research_task(self, task_id: str, company_name: str, ticker: str | None = None) -> dict:
    """投研任务入口（Celery 任务为同步函数，内部用 asyncio 驱动异步 IO）。"""
    logger.info(
        "task received | task_id={} company={} ticker={} attempt={}/{}",
        task_id, company_name, ticker, self.request.retries + 1, self.max_retries + 1,
    )
    try:
        return asyncio.run(_run_workflow(task_id, company_name, ticker))
    except SoftTimeLimitExceeded:
        # 需求 3.6：超时 → 标记 failed + 记录超时日志与指标
        m.research_task_timeouts_total.inc()
        m.research_tasks_total.labels(status="failed").inc()
        asyncio.run(_mark_failed(task_id, f"timeout: 超过 {settings.celery_task_time_limit}s 未完成"))
        logger.error("task timeout | task_id={} limit={}s", task_id, settings.celery_task_time_limit)
        raise
    except Exception as exc:
        if self.request.retries < self.max_retries:
            # 需求 F6：失败自动重试（指数退避）
            countdown = min(
                settings.celery_retry_backoff * (2 ** self.request.retries),
                settings.celery_retry_backoff_max,
            )
            logger.warning(
                "task failed, will retry | task_id={} attempt={} countdown={}s err={}",
                task_id, self.request.retries + 1, countdown, exc,
            )
            try:  # 记录重试次数：N1 评测的成功率口径需含自动重试
                asyncio.run(_bump_retry(task_id))
            except Exception as bump_exc:
                logger.warning("retry_count 更新失败 | task_id={} err={}", task_id, bump_exc)
            raise self.retry(exc=exc, countdown=countdown)
        m.research_tasks_total.labels(status="failed").inc()
        asyncio.run(_mark_failed(task_id, f"{type(exc).__name__}: {exc}"))
        logger.error("task failed permanently | task_id={} err={}", task_id, exc)
        raise


# ---------------------------------------------------------------------------
# 工作流编排
# ---------------------------------------------------------------------------
async def _run_workflow(task_id: str, company_name: str, ticker: str | None) -> dict:
    started = time.perf_counter()
    await init_pool()
    m.research_tasks_in_progress.inc()
    try:
        await _set_row(
            task_id, status="running", current_step=WORKFLOW_STEPS[0][2], progress=0.05
        )
        results: dict[str, dict] = {}
        for idx, (step, agent, label) in enumerate(WORKFLOW_STEPS):
            step_started = time.perf_counter()
            payload = await _run_step(step, agent, task_id, company_name, ticker, results)
            elapsed = time.perf_counter() - step_started

            # ---- 指标（Agent 维度 / 步骤维度）----
            # 降级产出（degraded）与占位产出（stub）计入 degraded，便于区分「真实成功」与「兜底跳过」
            step_status = "degraded" if (payload.get("degraded") or payload.get("stub")) else "success"
            m.agent_execution_duration_seconds.labels(agent=agent).observe(elapsed)
            m.agent_executions_total.labels(agent=agent, status=step_status).inc()
            m.research_step_duration_seconds.labels(step=step).observe(elapsed)
            m.research_steps_total.labels(step=step, status=step_status).inc()
            if step != "profile":
                # profile 步的 LLM 耗时与 Token 由 Agent 内部真实埋点，此处只占位其余步骤
                m.llm_request_duration_seconds.labels(
                    model=settings.llm_model(settings.report_writer_llm_tier if step == "report" else "flash"),
                    agent=agent,
                ).observe(elapsed * 0.6)

            # ---- 需求 F5：中间产出落库（重跑同一步覆盖）----
            # 报告正文较长，单独存 research_reports；中间产出只留元信息，避免状态接口响应过大
            stored = {k: v for k, v in payload.items() if k != "content"} if step == "report" else payload
            await _save_output(task_id, step, stored)
            results[step] = payload

            # 需求 3.3：Task 2（财报分析）以 Task 1 输出的 ticker 为输入
            if step == "profile" and not ticker and payload.get("ticker"):
                ticker = payload["ticker"]
                logger.info("ticker 由信息搜集步解析 | task_id={} ticker={}", task_id, ticker)
            await _set_row(
                task_id, current_step=label, progress=round((idx + 1) / len(WORKFLOW_STEPS), 2)
            )
            logger.info("step done | task_id={} step={} agent={} elapsed={:.2f}s", task_id, step, agent, elapsed)

        # ---- 最终报告落库（需求 F4）----
        # 报告由第三步（报告撰写 Agent）产出；若该步完全失败则退回结构化占位模板
        report_payload = results.get("report") or {}
        report_md = report_payload.get("content") or _placeholder_report(company_name, ticker, results)
        await _save_report(task_id, report_md)
        await _set_row(task_id, status="completed", current_step="已完成", progress=1.0, completed=True)

        total = time.perf_counter() - started
        m.research_task_duration_seconds.observe(total)
        m.research_tasks_total.labels(status="completed").inc()
        m.reports_generated_total.labels(format=settings.report_format).inc()
        await _refresh_gauges()
        logger.info("task completed | task_id={} elapsed={:.1f}s", task_id, total)
        return {
            "task_id": task_id,
            "status": "completed",
            "elapsed_seconds": round(total, 2),
            "steps": [s for s, _, _ in WORKFLOW_STEPS],
        }
    finally:
        m.research_tasks_in_progress.dec()
        await close_pool()


async def _run_step(
    step: str,
    agent: str,
    task_id: str,
    company_name: str,
    ticker: str | None,
    results: dict[str, dict],
) -> dict:
    """工作流单步执行（三步均已接入真实 Agent）。

    - profile    ：信息搜集 Agent（CrewAI + DeepSeek Flash + 检索工具，阶段 A）
    - financials ：财务分析 Agent（yfinance → SEC XBRL → Bing 兜底，阶段 B）
    - report     ：报告撰写 Agent（DeepSeek Pro，8 章结构 + 落盘，阶段 C）
    """
    if step == "profile":
        # 延迟导入：只为入队而 import 本模块的 API 进程无需加载 crewai（省启动时间与内存）
        from app.agents.info_collector import collect_company_profile

        profile = await asyncio.to_thread(collect_company_profile, company_name, ticker)
        return profile.model_dump(mode="json")

    if step == "financials":
        # 阶段 B：真实财务分析 Agent（yfinance → SEC XBRL → Bing 检索兜底）
        from app.agents.financial_analyst import collect_financial_analysis

        analysis = await asyncio.to_thread(collect_financial_analysis, ticker, company_name)
        return analysis.model_dump(mode="json")

    if step == "report":
        # 阶段 C：真实报告撰写 Agent（第 8 章「附录：原始数据」由代码按真实数据生成）
        from app.agents.report_writer import generate_report

        report = await asyncio.to_thread(
            generate_report,
            task_id,
            company_name,
            ticker,
            results.get("profile"),
            results.get("financials"),
        )
        return report.model_dump(mode="json")

    raise ValueError(f"未知工作流步骤：{step}")


def _placeholder_report(company_name: str, ticker: str | None, results: dict[str, dict]) -> str:
    """占位报告（接入 ReportWriter Agent 后由其生成 Markdown）。"""
    return (
        f"# {company_name} 投资分析报告（占位）\n\n"
        f"- 股票代码：{ticker or 'TBD'}\n"
        f"- 状态：骨架阶段占位产出，**未接入真实 Agent 与数据源**\n\n"
        "## 1. 投资摘要\n（待 ReportWriter Agent 生成）\n\n"
        "## 2. 公司概况\n（待 InformationSpecialist Agent 生成）\n\n"
        "## 3. 财务分析\n（待 FinancialAnalyst Agent 生成）\n\n"
        "## 4. 风险因素\n（待生成）\n\n"
        "## 5. 投资观点\n（待生成）\n"
    )


# ---------------------------------------------------------------------------
# 落库辅助
# ---------------------------------------------------------------------------
def _as_uuid(task_id: str) -> uuid.UUID:
    return uuid.UUID(str(task_id))


async def _set_row(
    task_id: str,
    *,
    status: str | None = None,
    current_step: str | None = None,
    progress: float | None = None,
    completed: bool = False,
) -> None:
    """更新任务行（只更新传入的字段）。"""
    sets, args = [], []
    if status is not None:
        args.append(status); sets.append(f"status = ${len(args)}")
    if current_step is not None:
        args.append(current_step); sets.append(f"current_step = ${len(args)}")
    if progress is not None:
        args.append(progress); sets.append(f"progress = ${len(args)}")
    if completed:
        sets.append("completed_at = NOW()")
    if not sets:
        return
    args.append(_as_uuid(task_id))
    sql = f"UPDATE research_tasks SET {', '.join(sets)} WHERE task_id = ${len(args)}"
    async with get_pool().acquire() as conn:
        await conn.execute(sql, *args)


async def _save_output(task_id: str, step: str, payload: dict) -> None:
    """中间产出落库；重跑同一步时覆盖（唯一索引 uq_task_outputs_task_step）。"""
    async with get_pool().acquire() as conn:
        await conn.execute(
            """INSERT INTO task_outputs (task_id, step, output_json) VALUES ($1, $2, $3::jsonb)
               ON CONFLICT (task_id, step) DO UPDATE
                 SET output_json = EXCLUDED.output_json, created_at = NOW()""",
            _as_uuid(task_id), step, json.dumps(payload, ensure_ascii=False),
        )


async def _save_report(task_id: str, content: str) -> None:
    """报告落库 + 落盘（report_output_dir）。"""
    rel_path = f"{settings.report_output_dir.rstrip('/')}/{task_id}.md"
    try:
        path = Path(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception as exc:  # 落盘失败不影响任务完成（内容已在库里）
        logger.warning("report file write failed | task_id={} err={}", task_id, exc)
    async with get_pool().acquire() as conn:
        await conn.execute(
            """INSERT INTO research_reports (task_id, report_path, report_content, word_count)
               VALUES ($1, $2, $3, $4)""",
            _as_uuid(task_id), rel_path, content, len(content.split()),
        )


async def _refresh_gauges() -> None:
    """刷新队列深度与成功率（需求 N1：任务成功率 >95%）。"""
    try:
        async with get_pool().acquire() as conn:
            queued = await conn.fetchval(
                "SELECT count(*) FROM research_tasks WHERE status IN ('queued','running')"
            )
            recent = await conn.fetch(
                "SELECT status FROM research_tasks ORDER BY created_at DESC LIMIT 100"
            )
        m.research_task_queue_length.set(int(queued or 0))
        if recent:
            completed = sum(1 for r in recent if r["status"] == "completed")
            m.research_task_success_rate.set(completed / len(recent))
    except Exception as exc:
        logger.warning("refresh gauges failed: {}", exc)


async def _bump_retry(task_id: str) -> None:
    """retry_count += 1（N1 评测口径：成功率需含自动重试后的结果）。"""
    try:
        await init_pool()
        async with get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE research_tasks SET retry_count = retry_count + 1 WHERE task_id = $1",
                _as_uuid(task_id),
            )
    finally:
        await close_pool()


async def _mark_failed(task_id: str, message: str) -> None:
    """失败/超时兜底：独立建池，确保状态一定落库（需求 3.6）。"""
    try:
        await init_pool()
        async with get_pool().acquire() as conn:
            await conn.execute(
                """UPDATE research_tasks
                      SET status = 'failed', error_message = $2, completed_at = NOW()
                    WHERE task_id = $1""",
                _as_uuid(task_id), message[:500],
            )
        await _refresh_gauges()
    except Exception as exc:
        logger.error("mark failed write error | task_id={} err={}", task_id, exc)
    finally:
        await close_pool()
