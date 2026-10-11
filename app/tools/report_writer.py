"""报告落盘工具（需求 3.4 Agent 3：FileWriterTool）。

职责：把最终 Markdown 报告写入 ``settings.report_output_dir/{task_id}.md``，
返回 (相对路径, 字数)，供 research_reports 落库与 API 返回。

说明：同时提供 CrewAI 工具封装（ReportWriterTool），但生产链路里由
``app/agents/report_writer.py`` 直接调用 ``write_report_file()`` ——
报告正文是「LLM 生成 + 代码追加真实数据附录」合成后的结果，
交给代码写文件比让 LLM 自己调用工具写文件更可靠（不会漏写/截断）。
"""
from __future__ import annotations

import json
from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from app.config import settings
from app.core import metrics as m
from app.core.logging import logger


def report_path(task_id: str) -> str:
    """报告相对路径（统一入口，避免各处拼接不一致）。"""
    return f"{settings.report_output_dir.rstrip('/')}/{task_id}.md"


def write_report_file(task_id: str, content: str) -> tuple[str, int]:
    """写入报告文件，返回 (相对路径, 字数)。"""
    rel_path = report_path(task_id)
    path = Path(rel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    words = len(content.split())
    logger.info("报告已落盘 | task_id={} path={} chars={} words={}", task_id, rel_path, len(content), words)
    m.tool_calls_total.labels(tool="report_writer", status="success").inc()
    return rel_path, words


class _ReportInput(BaseModel):
    task_id: str = Field(..., description="任务 ID，用于命名报告文件")
    content: str = Field(..., description="完整的 Markdown 报告正文")


class ReportWriterTool(BaseTool):
    name: str = "report_writer"
    description: str = (
        "把完整的 Markdown 投资分析报告写入 data/reports/{task_id}.md 并返回文件路径。"
        "输入 task_id 与报告正文（Markdown 全文）。"
    )
    args_schema: type[BaseModel] = _ReportInput

    def _run(self, task_id: str, content: str) -> str:
        try:
            rel_path, words = write_report_file(task_id, content)
        except Exception as exc:
            m.tool_calls_total.labels(tool="report_writer", status="failed").inc()
            logger.error("报告落盘失败 | task_id={} err={}", task_id, exc)
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
        return json.dumps({"report_path": rel_path, "word_count": words}, ensure_ascii=False)
