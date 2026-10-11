"""报告撰写 Agent（需求 3.4 Agent 3：投资报告撰写专家）。

结构（8 章，需求 3.4）：
    1 投资摘要 · 2 公司概况 · 3 行业分析 · 4 财务分析 · 5 核心财务指标 · 6 风险因素 · 7 投资观点
    8 附录：原始数据 ← **由代码按真实 JSON 生成**（保证数字零失真，不交给 LLM 誊抄）

LLM：DeepSeek **V4 Pro**（settings.report_writer_llm_tier=pro，质量优先）

降级策略（需求 3.7 / 3.9）：
    - 前序步骤有 degraded=true → 报告顶部插入「数据来源受限」警示，并置 degraded=true
    - LLM 超时 / 输出缺少章节 → 重试（settings.step_max_retries 次）；仍失败 → 结构化模板（占位符），
      附录仍是真实数据，任务不失败
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from typing import Any

from crewai import Agent, Crew, LLM, Process, Task
from pydantic import BaseModel, Field

from app.config import settings
from app.core import metrics as m
from app.core.logging import logger
from app.tools.report_writer import write_report_file

# 关闭 CrewAI 执行追踪（与前两个 Agent 一致：headless 环境必须非交互）
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

AGENT_NAME = "ReportWriter"
STUB_STEP = "report"

# LLM 负责的 7 章（第 8 章由代码追加）
SECTIONS: list[tuple[int, str]] = [
    (1, "投资摘要"),
    (2, "公司概况"),
    (3, "行业分析"),
    (4, "财务分析"),
    (5, "核心财务指标"),
    (6, "风险因素"),
    (7, "投资观点"),
]


class ReportResult(BaseModel):
    task_id: str
    content: str
    report_path: str | None = None
    word_count: int = 0
    char_count: int = 0
    sections: list[str] = Field(default_factory=list)
    llm_ok: bool = False
    degraded: bool = False
    error: str | None = None
    model: str | None = None
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 数值格式化（统一「亿美元」口径，便于 LLM 直接引用）
# ---------------------------------------------------------------------------
def _yi(value: Any) -> str:
    return f"{value / 1e8:,.2f}" if isinstance(value, (int, float)) else "—"


def _num(value: Any, digits: int = 2) -> str:
    return f"{value:,.{digits}f}" if isinstance(value, (int, float)) else "—"


def _pct(value: Any) -> str:
    return f"{value * 100:,.2f}%" if isinstance(value, (int, float)) else "—"


# ---------------------------------------------------------------------------
# 数据包（给 LLM 的事实依据）
# ---------------------------------------------------------------------------
def _build_data_pack(profile: dict, financials: dict) -> str:
    lines: list[str] = []

    lines.append("【公司档案】")
    lines.append(
        f"公司名称: {profile.get('company_name') or '未知'} | 股票代码: {profile.get('ticker') or '未知'} "
        f"| 行业: {profile.get('sector') or '未知'}"
    )
    if profile.get("description"):
        lines.append(f"公司简介: {profile['description']}")
    if profile.get("products"):
        lines.append("主要产品: " + "、".join(str(p) for p in profile["products"][:15]))
    if profile.get("competitors"):
        lines.append("竞争对手: " + "、".join(str(c) for c in profile["competitors"][:15]))
    news = profile.get("recent_news") or []
    if news:
        lines.append("近期新闻:")
        for item in news[:5]:
            title = item.get("title") if isinstance(item, dict) else str(item)
            date = (item.get("date") if isinstance(item, dict) else "") or "日期未标注"
            lines.append(f"  - ({date}) {title}")

    years = financials.get("fiscal_years") or []
    income = financials.get("income_statement") or {}
    balance = financials.get("balance_sheet") or {}
    cash = financials.get("cash_flow") or {}
    if years:
        lines.append("")
        lines.append("【财务数据】（单位：亿美元，括号内为原始美元值）")
        for year in years:
            inc, bal, cf = income.get(year, {}), balance.get(year, {}), cash.get(year, {})
            lines.append(
                f"FY{year}: 营业收入 {_yi(inc.get('revenue'))} 亿美元 | 净利润 {_yi(inc.get('net_income'))} "
                f"| 毛利 {_yi(inc.get('gross_profit'))} | 营业利润 {_yi(inc.get('operating_income'))} "
                f"| EPS {_num(inc.get('eps'))} 美元"
            )
            lines.append(
                f"        经营现金流 {_yi(cf.get('operating_cash_flow'))} | 资本开支 {_yi(cf.get('capex'))} "
                f"| 自由现金流 {_yi(cf.get('free_cash_flow'))} | 总资产 {_yi(bal.get('total_assets'))} "
                f"| 总负债 {_yi(bal.get('total_liabilities'))} | 股东权益 {_yi(bal.get('stockholders_equity'))}"
            )
        latest = income.get(years[0], {})
        if latest.get("revenue"):
            lines.append(f"原始值示例: FY{years[0]} 营业收入 = {latest['revenue']:,.0f} 美元")

    metrics = financials.get("key_metrics") or {}
    shown = {k: v for k, v in metrics.items() if v is not None}
    if shown:
        lines.append("")
        lines.append("【核心指标】")
        lines.append(
            " | ".join(
                f"{k}={_pct(v) if ('margin' in k or 'growth' in k or k == 'roe' or 'ratio' in k) else _num(v)}"
                for k, v in shown.items()
            )
        )

    if financials.get("market_cap") or financials.get("stock_price"):
        lines.append("")
        lines.append(
            f"【市场数据】股价 {_num(financials.get('stock_price'))} 美元 | "
            f"市值 {_yi(financials.get('market_cap'))} 亿美元 "
            f"（报表来源 {financials.get('data_source')}，报价来源 {financials.get('quote_source')}）"
        )

    if financials.get("summary") or financials.get("trends"):
        lines.append("")
        lines.append("【财务分析 Agent 的既有结论】（可直接引用）")
        if financials.get("summary"):
            lines.append(f"概述: {financials['summary']}")
        for trend in (financials.get("trends") or [])[:5]:
            lines.append(f"  - {trend}")

    lines.append("")
    lines.append("【数据限制】")
    limits: list[str] = []
    if profile.get("degraded"):
        limits.append(f"公司档案降级：{profile.get('error') or '未取得公司信息'}")
    if financials.get("degraded"):
        limits.append(f"财务数据降级：{financials.get('error') or '未取得财务数据'}")
    if not years:
        limits.append("没有任何财年财务数据")
    lines.append("；".join(limits) if limits else "无（两份数据均完整）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 第 8 章：附录（代码生成，保证与数据库数字完全一致）
# ---------------------------------------------------------------------------
def _appendix(profile: dict, financials: dict, model: str) -> str:
    years = financials.get("fiscal_years") or []
    income = financials.get("income_statement") or {}
    balance = financials.get("balance_sheet") or {}
    cash = financials.get("cash_flow") or {}
    metrics = financials.get("key_metrics") or {}

    out = ["", "---", "", "## 8. 附录：原始数据", ""]
    out.append("> 本章由系统按采集到的原始数据直接生成（未经语言模型改写），单位统一为**亿美元**。")

    def table(title: str, rows: list[tuple[str, str]], note: str = "") -> None:
        out.append("")
        out.append(f"### {title}")
        if note:
            out.append(note)
        if not years:
            out.append("（无数据）")
            return
        out.append("| 科目 | " + " | ".join(f"FY{y}" for y in years) + " |")
        out.append("|---" * (len(years) + 1) + "|")
        for label, field in rows:
            values = []
            for year in years:
                source = income if field in ("revenue", "net_income", "gross_profit", "operating_income", "eps") else (
                    balance if field in ("total_assets", "total_liabilities", "stockholders_equity", "total_debt", "cash") else cash
                )
                value = source.get(year, {}).get(field)
                values.append(_num(value) if field == "eps" else _yi(value))
            out.append(f"| {label} | " + " | ".join(values) + " |")

    table(
        "8.1 利润表",
        [
            ("营业收入", "revenue"),
            ("毛利", "gross_profit"),
            ("营业利润", "operating_income"),
            ("净利润", "net_income"),
            ("每股收益（美元）", "eps"),
        ],
    )
    table(
        "8.2 资产负债表",
        [
            ("总资产", "total_assets"),
            ("总负债", "total_liabilities"),
            ("股东权益", "stockholders_equity"),
            ("有息负债", "total_debt"),
            ("现金及等价物", "cash"),
        ],
    )
    table(
        "8.3 现金流量表",
        [
            ("经营活动现金流", "operating_cash_flow"),
            ("资本开支", "capex"),
            ("自由现金流", "free_cash_flow"),
        ],
    )

    out += ["", "### 8.4 核心指标", ""]
    if metrics:
        out += ["| 指标 | 数值 |", "|---|---|"]
        for key, value in metrics.items():
            if value is None:
                continue
            rendered = _pct(value) if ("margin" in key or "growth" in key or key in ("roe",)) else _num(value)
            if "ratio" in key or "liabilit" in key:
                rendered = _num(value)
            out.append(f"| {key} | {rendered} |")
    else:
        out.append("（无数据）")

    out += ["", "### 8.5 数据来源与限制", ""]
    out.append(f"- 公司档案：{profile.get('data_sources') or '—'}（degraded={bool(profile.get('degraded'))}）")
    out.append(
        f"- 财务报表：{financials.get('data_source') or '—'}；行情报价：{financials.get('quote_source') or '—'}"
    )
    out.append(f"- 报告生成模型：{model}")
    if profile.get("degraded"):
        out.append(f"- ⚠️ 公司档案降级：{profile.get('error') or '未取得'}")
    if financials.get("degraded"):
        out.append(f"- ⚠️ 财务数据降级：{financials.get('error') or '未取得'}")
    if not (profile.get("degraded") or financials.get("degraded")):
        out.append("- 本报告数据来自公开渠道，未发现降级项")
    return "\n".join(out)


def _header(company_name: str, ticker: str | None, degraded_flags: list[str], llm_ok: bool) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# {company_name}（{ticker or 'N/A'}）投资分析报告",
        "",
        f"> 生成时间：{now} ｜ 生成方式：DeepSeek {settings.llm_model(settings.report_writer_llm_tier)}"
        f"{'（正文）' if llm_ok else '（未生效，正文为结构化模板）'}",
        "",
    ]
    if degraded_flags:
        lines += [
            "> ⚠️ **数据来源受限**：以下上游数据存在降级，报告结论的可靠性下降，请结合原始数据自行判断。",
            "",
        ]
        lines += [f"> - {flag}" for flag in degraded_flags]
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 结构化模板（LLM 不可用时的占位兜底）
# ---------------------------------------------------------------------------
def _template_body(company_name: str, ticker: str | None, profile: dict, financials: dict, reason: str | None) -> str:
    years = financials.get("fiscal_years") or []
    latest_rev = (financials.get("income_statement") or {}).get(years[0], {}).get("revenue") if years else None
    metrics = financials.get("key_metrics") or {}
    description = profile.get("description") or "未取得公司简介"

    def placeholder(text: str = "（待补充：本次未能生成该部分内容）") -> str:
        return text

    parts = [
        "## 1. 投资摘要",
        placeholder(
            f"{company_name}（{ticker or 'N/A'}）"
            + (f"最近财年营业收入 {_yi(latest_rev)} 亿美元，" if latest_rev else "未取得财务数据，")
            + f"核心指标 PE={_num(metrics.get('pe_ratio'))}、ROE={_pct(metrics.get('roe'))}。"
            "（本节为结构化占位，需人工补充或重跑）"
        ),
        "",
        "## 2. 公司概况",
        description,
        "",
        "## 3. 行业分析",
        placeholder(),
        "",
        "## 4. 财务分析",
        placeholder(
            "财务数据已采集，见第 8 章附录；分析正文未能生成。"
            if years
            else "未取得财务数据，无法进行财务分析。"
        ),
        "",
        "## 5. 核心财务指标",
        "、".join(f"{k}={_num(v)}" for k, v in metrics.items() if v is not None) or "未取得核心指标。",
        "",
        "## 6. 风险因素",
        placeholder("数据不足，无法评估风险因素。"),
        "",
        "## 7. 投资观点",
        placeholder("数据不足，无法给出投资观点。"),
    ]
    if reason:
        parts += ["", f"> 正文降级原因：{reason}"]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM / Agent
# ---------------------------------------------------------------------------
def build_llm() -> LLM:
    if not settings.llm_ready:
        raise RuntimeError("DeepSeek 未配置：请在 .env / .env.local 填写 DEEPSEEK_API_KEY")
    return LLM(
        model=f"openai/{settings.llm_model(settings.report_writer_llm_tier)}",
        base_url=settings.deepseek_base_url,
        api_key=settings.deepseek_api_key,
        temperature=settings.llm_temperature,
        timeout=settings.report_llm_timeout,
        max_tokens=settings.report_max_tokens,
    )


def build_report_agent(llm: LLM | None = None) -> Agent:
    """投资报告撰写专家（需求 3.4 的角色设定）。"""
    return Agent(
        role="投资报告撰写专家",
        goal="综合公司档案与财务数据，撰写结构清晰、论据充分、有投资价值的分析报告",
        backstory=(
            "你是一位顶尖的投资银行报告主笔，撰写的报告被机构投资者广泛阅读。"
            "你擅长将复杂的财务数据转化为易懂的投资观点，你的报告风格专业、客观、有洞察力。"
        ),
        llm=llm or build_llm(),
        tools=[],              # 报告正文不依赖外部工具；原始数据由系统直接注入
        allow_delegation=False,
        max_iter=2,
        verbose=False,
    )


def _section_prompt(data_pack: str, previous_error: str | None = None) -> str:
    headings = "\n".join(f"## {idx}. {title}" for idx, title in SECTIONS)
    parts = [
        f"请基于下方**真实数据**撰写一份投资分析报告，只输出 Markdown 正文，必须且只能包含这 7 个二级标题：",
        headings,
        "",
        "写作要求：",
        "1. 所有数字必须来自下方数据，**禁止编造或推测**；引用时使用数据中给出的「亿美元」口径"
        "（例如「营业收入 4161.61 亿美元」「PE 40.57」「ROE 151.91%」）；",
        "2. 第 4 章「财务分析」需覆盖收入分析、盈利能力、现金流、资产负债表四个方面；",
        "3. 第 5 章「核心财务指标」用 Markdown 表格列出 PE / ROE / 毛利率 / 净利率 / 营收增长率等；",
        "4. 数据缺失的部分明确写「该部分数据不足，无法分析」，不得虚构；",
        "5. 不要输出第 8 章（原始数据附录由系统追加），不要输出任何解释性前言或后记；",
        "6. 全文使用中文，语气专业客观。",
        "",
        "===== 数据 =====",
        data_pack,
    ]
    if previous_error:
        parts += [
            "",
            f"⚠️ 上一次输出不合格（{previous_error}）。请务必输出完整的 7 个章节标题与正文。",
        ]
    return "\n".join(parts)


def _run_crew(prompt: str) -> str:
    agent = build_report_agent()
    task = Task(description=prompt, expected_output="包含 7 个二级标题的完整 Markdown 报告正文", agent=agent)
    crew = Crew(
        agents=[agent], tasks=[task], process=Process.sequential, verbose=False, tracing=False
    )
    started = time.perf_counter()
    result = crew.kickoff()
    elapsed = time.perf_counter() - started

    model = settings.llm_model(settings.report_writer_llm_tier)
    m.llm_request_duration_seconds.labels(model=model, agent=AGENT_NAME).observe(elapsed)
    usage = getattr(result, "token_usage", None)
    if usage is not None:
        if getattr(usage, "prompt_tokens", None):
            m.llm_tokens_total.labels(model=model, type="prompt").inc(int(usage.prompt_tokens))
        if getattr(usage, "completion_tokens", None):
            m.llm_tokens_total.labels(model=model, type="completion").inc(int(usage.completion_tokens))
    logger.info("report agent finished | elapsed={:.1f}s tokens={}", elapsed, usage)
    return getattr(result, "raw", None) or str(result)


def _missing_sections(body: str) -> list[str]:
    """检查 7 个章节标题是否齐全（容忍标题级别与分隔符差异）。"""
    missing: list[str] = []
    for idx, title in SECTIONS:
        pattern = rf"#+\s*{idx}\s*[.、·]?\s*{re.escape(title)}"
        if not re.search(pattern, body):
            missing.append(f"{idx}. {title}")
    return missing


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def generate_report(
    task_id: str,
    company_name: str,
    ticker: str | None = None,
    profile: dict | None = None,
    financials: dict | None = None,
) -> ReportResult:
    """报告撰写主入口（同步；由 Celery 任务经 asyncio.to_thread 调用）。"""
    started = time.perf_counter()
    profile = profile or {}
    financials = financials or {}
    model = settings.llm_model(settings.report_writer_llm_tier)

    degraded_flags: list[str] = []
    if profile.get("degraded"):
        degraded_flags.append(f"公司档案：{profile.get('error') or 'degraded=true'}")
    if financials.get("degraded"):
        degraded_flags.append(f"财务数据：{financials.get('error') or 'degraded=true'}")

    data_pack = _build_data_pack(profile, financials)

    # ---- LLM 生成正文（7 章），失败重试后落模板 ----
    llm_ok = False
    body = ""
    error: str | None = None
    attempts = max(1, settings.step_max_retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            raw = _run_crew(_section_prompt(data_pack, error))
            candidate = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", raw.strip())
            missing = _missing_sections(candidate)
            if missing:
                raise ValueError(f"缺少章节：{', '.join(missing)}")
            body = candidate
            llm_ok = True
            if attempt > 1:
                m.research_step_retries_total.labels(step=STUB_STEP).inc()
            break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            m.agent_validation_failures_total.labels(agent=AGENT_NAME).inc()
            logger.warning(
                "report attempt failed | task_id={} attempt={}/{} err={}",
                task_id, attempt, attempts, error[:180],
            )

    if not llm_ok:
        logger.error("报告正文降级为结构化模板 | task_id={} err={}", task_id, error)
        body = _template_body(company_name, ticker, profile, financials, error)
        degraded_flags.append(f"报告正文：{error or 'LLM 未返回可用内容'}")

    content = "\n".join(
        [
            _header(company_name, ticker, degraded_flags, llm_ok),
            body,
            _appendix(profile, financials, model),
            "",
        ]
    )

    report_file, words = write_report_file(task_id, content)
    result = ReportResult(
        task_id=task_id,
        content=content,
        report_path=report_file,
        word_count=words,
        char_count=len(content),
        sections=[title for _, title in SECTIONS] + ["附录：原始数据"],
        llm_ok=llm_ok,
        degraded=bool(degraded_flags),
        error=error if not llm_ok else None,
        model=model,
        notes=degraded_flags,
    )
    m.tool_calls_total.labels(tool="report_generation", status="success" if llm_ok else "degraded").inc()
    logger.info(
        "report generated | task_id={} llm_ok={} degraded={} chars={} 章节={} elapsed={:.1f}s",
        task_id, llm_ok, result.degraded, len(content), len(result.sections), time.perf_counter() - started,
    )
    return result
