"""财务分析 Agent（需求 3.4 Agent 2：财务分析师 / 3.3 Task 2）。

流水线：
    1) 结构化数据：yfinance（主）→ SEC EDGAR XBRL（备用；需求 3.9 允许 SEC ↔ Yahoo 互为降级源）
    2) 报价：随结构化数据一起取（yfinance → 新浪 → 腾讯，均免 Key）
    3) 指标：compute_ratios() 计算 PE / ROE / 毛利率 / 净利率 / 营收增长率 / 负债率
       （等价需求 3.4 的 FinancialCalculatorTool）
    4) LLM（DeepSeek Flash）：依据**真实数字**生成 trends / summary；
       若结构化数据源全部失败 → 用 Bing 检索片段兜底抽取数字
       （硬约束：片段中未出现的数字一律填 null，禁止推测）
    5) Pydantic 校验（FinancialAnalysis）；失败最多重试 settings.step_max_retries 次（需求 3.7）
    6) 仍失败 → degraded=True + error，上层记录错误并跳过该步（需求 3.7）

⚠️ 本机实测：Yahoo 对该出口 IP 持续限流，故实际走 SEC XBRL + 国内行情源；
   换到可访问 Yahoo 的网络后 yfinance 会自动恢复为主源。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from crewai import Agent, Crew, LLM, Process, Task
from pydantic import BaseModel, Field, model_validator

from app.config import settings
from app.core import metrics as m
from app.core.logging import logger
from app.tools.sec_financials import SecFinancialsTool, fetch_sec_financials
from app.tools.ticker_lookup import TickerLookupTool
from app.tools.web_search import BingSearchTool, search_web
from app.tools.yahoo_finance import (
    FinancialMetrics,
    TickerNoData,
    YahooUnavailable,
    YahooFinanceTool,
    fetch_financials,
)

# 关闭 CrewAI 执行追踪（与信息搜集 Agent 一致：headless 环境必须非交互）
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

AGENT_NAME = "FinancialAnalyst"
STUB_STEP = "financials"


# ---------------------------------------------------------------------------
# 输出模型（需求 3.3 Task 2：financial_metrics.json）
# ---------------------------------------------------------------------------
class FinancialAnalysis(FinancialMetrics):
    trends: list[str] = Field(default_factory=list)
    summary: str = ""

    @model_validator(mode="after")
    def _require_data(self) -> "FinancialAnalysis":
        """需求 3.7：真正触发重试的校验 —— 非降级结果必须有可用数据。"""
        if not self.degraded and not self.has_any_data():
            raise ValueError("既无报表数据也无关键指标：应标记 degraded=true 或补齐数据")
        return self


class _Interpretation(BaseModel):
    trends: list[str] = Field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# LLM / Agent 构建
# ---------------------------------------------------------------------------
def build_llm(tier: str = "flash") -> LLM:
    if not settings.llm_ready:
        raise RuntimeError("DeepSeek 未配置：请在 .env / .env.local 填写 DEEPSEEK_API_KEY")
    return LLM(
        model=f"openai/{settings.llm_model(tier)}",
        base_url=settings.deepseek_base_url,
        api_key=settings.deepseek_api_key,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout,
        max_tokens=settings.llm_max_tokens,
    )


def build_financial_agent(llm: LLM | None = None) -> Agent:
    """财务分析师（需求 3.4 的角色设定）。"""
    return Agent(
        role="财务分析师",
        goal="从公司财报数据中提取关键财务数据，计算并解读核心财务指标",
        backstory=(
            "你是一位专业的财务分析师，持有 CFA 证书。你精通财务报表分析、财务比率计算和"
            "财务建模，能从复杂的财务数据中识别趋势和风险点。"
        ),
        llm=llm or build_llm("flash"),
        tools=[
            YahooFinanceTool(),
            SecFinancialsTool(),
            BingSearchTool(),
            TickerLookupTool(),
        ],
        allow_delegation=False,
        max_iter=3,
        verbose=False,
    )


def _run_crew(description: str, expected_output: str) -> str:
    agent = build_financial_agent()
    task = Task(description=description, expected_output=expected_output, agent=agent)
    crew = Crew(
        agents=[agent], tasks=[task], process=Process.sequential, verbose=False, tracing=False
    )
    started = time.perf_counter()
    result = crew.kickoff()
    elapsed = time.perf_counter() - started
    model = settings.llm_model("flash")
    m.llm_request_duration_seconds.labels(model=model, agent=AGENT_NAME).observe(elapsed)
    usage = getattr(result, "token_usage", None)
    if usage is not None:
        if getattr(usage, "prompt_tokens", None):
            m.llm_tokens_total.labels(model=model, type="prompt").inc(int(usage.prompt_tokens))
        if getattr(usage, "completion_tokens", None):
            m.llm_tokens_total.labels(model=model, type="completion").inc(int(usage.completion_tokens))
    logger.info("financial agent finished | elapsed={:.1f}s tokens={}", elapsed, usage)
    return getattr(result, "raw", None) or str(result)


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        raise ValueError(f"输出中不含 JSON：{(text or '')[:150]!r}")
    return json.loads(match.group(0))


# ---------------------------------------------------------------------------
# 数据源 1/2：结构化财务数据
# ---------------------------------------------------------------------------
def _compact_numbers(data: FinancialMetrics) -> str:
    """把结构化数据压缩成给 LLM 的数字清单（省 token 且避免它去读长 JSON）。"""
    lines: list[str] = [f"ticker={data.ticker} 货币={data.currency} 数据源={data.data_source}"]
    for year in data.fiscal_years:
        inc = data.income_statement.get(year, {})
        bal = data.balance_sheet.get(year, {})
        cf = data.cash_flow.get(year, {})
        lines.append(
            f"{year}: 营收={inc.get('revenue')} 净利润={inc.get('net_income')} "
            f"毛利={inc.get('gross_profit')} 经营现金流={cf.get('operating_cash_flow')} "
            f"总资产={bal.get('total_assets')} 总负债={bal.get('total_liabilities')} "
            f"股东权益={bal.get('stockholders_equity')}"
        )
    lines.append(
        "关键指标: "
        + ", ".join(f"{k}={v}" for k, v in data.key_metrics.items() if v is not None)
    )
    lines.append(f"股价={data.stock_price} 市值={data.market_cap}（报价来源={data.quote_source}）")
    return "\n".join(lines)


def _interpret(data: FinancialMetrics) -> _Interpretation:
    """依据真实数字生成 trends / summary（失败不影响数据，仅留空）。"""
    numbers = _compact_numbers(data)
    description = (
        "下面是某公司已通过工具取回的真实财务数据（不要修改数字，也不要编造）：\n\n"
        f"{numbers}\n\n"
        "请只输出一个 JSON 对象，包含两个字段：\n"
        '{"summary": "3-5 句话的财务表现概述（必须引用上面的真实数字）", '
        '"trends": ["3-4 条趋势或风险点，每条一句话，基于上面的数字"]}\n'
        "要求：只依据上面的数字；不得出现上面没有的数字；不要输出 Markdown 代码块之外的任何文字。"
    )
    attempts = max(1, settings.step_max_retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            raw = _run_crew(description, "一个严格的 JSON 对象（summary + trends）")
            parsed = _Interpretation.model_validate(_extract_json(raw))
            if attempt > 1:
                m.research_step_retries_total.labels(step=STUB_STEP).inc()
            return parsed
        except Exception as exc:
            m.agent_validation_failures_total.labels(agent=AGENT_NAME).inc()
            logger.warning(
                "financial interpretation attempt {} failed | err={}", attempt, f"{type(exc).__name__}: {exc}"[:160]
            )
    return _Interpretation()


# ---------------------------------------------------------------------------
# 数据源 3：Bing 检索 + LLM 兜底抽取
# ---------------------------------------------------------------------------
_BING_QUERIES = (
    "{ticker} {company} revenue net income annual report",
    "{ticker} {company} PE ratio ROE gross margin market cap",
    "{ticker} {company} total assets debt cash flow",
)


def _search_snippets(ticker: str, company_name: str) -> tuple[str, list[str]]:
    blocks: list[str] = []
    sources: list[str] = []
    for template in _BING_QUERIES:
        query = template.format(ticker=ticker, company=company_name or "").strip()
        hit = search_web(query, max_results=5, prefer="duckduckgo")
        if hit["results"]:
            sources.append(hit["backend"] or "")
            lines = [f"[检索：{query}]"]
            lines += [
                f"{i}. {r['title']} — {r['snippet']} ({r['url']})"
                for i, r in enumerate(hit["results"], 1)
            ]
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks), list(dict.fromkeys(s for s in sources if s))


def _extract_from_search(ticker: str, company_name: str) -> FinancialMetrics | None:
    """结构化数据全失败时的最后一档：Bing 片段 → LLM 严格抽取。"""
    context, sources = _search_snippets(ticker, company_name)
    if not context:
        logger.warning("bing fallback 无检索结果 | ticker={}", ticker)
        return None

    description = (
        f"下面是关于 {ticker}（{company_name or '未知公司名'}）的网页检索片段。\n"
        "请**只使用片段中明确出现的数字**抽取财务数据；片段中没出现的字段一律填 null，"
        "严禁根据常识或记忆补数字。\n\n"
        "只输出一个 JSON 对象：\n"
        "{\n"
        '  "fiscal_years": ["2025", "2024", "2023"],\n'
        '  "income_statement": {"2025": {"revenue": null, "net_income": null, "gross_profit": null}, "...": {}},\n'
        '  "balance_sheet": {"2025": {"total_assets": null, "total_liabilities": null, "stockholders_equity": null}, "...": {}},\n'
        '  "cash_flow": {"2025": {"operating_cash_flow": null}, "...": {}},\n'
        '  "key_metrics": {"pe_ratio": null, "roe": null, "gross_margin": null, "revenue_growth_yoy": null},\n'
        '  "market_cap": null,\n'
        '  "stock_price": null,\n'
        '  "evidence": ["引用到的原文短句（含数字）"]\n'
        "}\n\n"
        "===== 检索片段 =====\n"
        f"{context}"
    )
    try:
        raw = _run_crew(description, "一个严格符合结构的 JSON 对象（缺失字段填 null）")
        payload = _extract_json(raw)
    except Exception as exc:
        logger.warning("bing fallback 抽取失败 | ticker={} err={}", ticker, f"{type(exc).__name__}: {exc}"[:160])
        return None

    def _clean_statement(node: Any) -> dict[str, dict[str, float | None]]:
        out: dict[str, dict[str, float | None]] = {}
        if not isinstance(node, dict):
            return out
        for year, values in node.items():
            if isinstance(values, dict):
                out[str(year)] = {
                    str(k): (float(v) if isinstance(v, (int, float)) else None)
                    for k, v in values.items()
                }
        return out

    income = _clean_statement(payload.get("income_statement"))
    balance = _clean_statement(payload.get("balance_sheet"))
    cash_flow = _clean_statement(payload.get("cash_flow"))
    raw_metrics = payload.get("key_metrics") if isinstance(payload.get("key_metrics"), dict) else {}
    key_metrics = {
        str(k): (float(v) if isinstance(v, (int, float)) else None) for k, v in raw_metrics.items()
    }
    years = [str(y) for y in (payload.get("fiscal_years") or list(income.keys()))][:3]
    market_cap = payload.get("market_cap") if isinstance(payload.get("market_cap"), (int, float)) else None
    price = payload.get("stock_price") if isinstance(payload.get("stock_price"), (int, float)) else None

    # 兜底：即使 LLM 没抽到报表，也尽量补一个可达的报价（新浪/腾讯）
    if price is None or market_cap is None:
        from app.tools.yahoo_finance import fetch_quote

        quote = fetch_quote(ticker)
        price = price if price is not None else quote.get("stock_price")
        market_cap = market_cap if market_cap is not None else quote.get("market_cap")

    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
    if not evidence:
        # 无任何引用证据 → 拒绝 LLM 抽取的全部数字，避免「没有出处的数字」被当成真实财务数据。
        # 此时仅保留后面重新取到的行情（若有），否则整步会走降级。
        logger.warning("bing fallback 无引用证据，丢弃 LLM 抽取的数字 | ticker={}", ticker)
        income, balance, cash_flow, key_metrics, years = {}, {}, {}, {}, []
        market_cap = price = None
    logger.info(
        "bing fallback 完成 | ticker={} years={} 证据条数={}", ticker, years, len(evidence)
    )
    return FinancialMetrics(
        ticker=ticker.upper(),
        fiscal_years=years,
        income_statement=income,
        balance_sheet=balance,
        cash_flow=cash_flow,
        key_metrics=key_metrics,
        market_cap=market_cap,
        stock_price=price,
        data_source="bing",
        quote_source="sina/tencent" if price is not None else None,
        notes=[f"结构化数据源不可用，改用检索片段抽取（来源：{', '.join(sources) or 'bing'}）"]
        + ([f"引用证据：{evidence[0]}"] if evidence else []),
    )


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def collect_financial_analysis(ticker: str | None, company_name: str = "") -> FinancialAnalysis:
    """财务分析主入口（同步；由 Celery 任务经 asyncio.to_thread 调用）。"""
    started = time.perf_counter()
    notes: list[str] = []

    if not ticker:
        return FinancialAnalysis(
            ticker="",
            data_source="none",
            degraded=True,
            error="缺少 ticker：信息搜集步骤未解析出股票代码，无法进行财务分析",
        )

    base: FinancialMetrics | None = None

    # 1) yfinance（主数据源）
    try:
        base = fetch_financials(ticker, years=3)
    except (YahooUnavailable, TickerNoData) as exc:
        notes.append(f"yfinance 不可用：{type(exc).__name__}: {exc}")
        logger.warning("yfinance 路径失败 | ticker={} err={}", ticker, f"{type(exc).__name__}: {exc}"[:140])
    except Exception as exc:
        notes.append(f"yfinance 异常：{type(exc).__name__}: {exc}")

    # 2) SEC XBRL（需求 3.9：SEC ↔ Yahoo 互为备用）
    if base is None or not base.has_statements():
        try:
            base = fetch_sec_financials(ticker, years=3)
        except Exception as exc:
            notes.append(f"SEC XBRL 不可用：{type(exc).__name__}: {exc}")
            logger.warning("SEC 路径失败 | ticker={} err={}", ticker, f"{type(exc).__name__}: {exc}"[:140])

    # 3) Bing 检索兜底
    search_attempted = False
    if base is None or (not base.has_statements() and not base.has_any_data()):
        search_attempted = True
        fallback = _extract_from_search(ticker, company_name)
        if fallback is not None:
            base = fallback

    # 4) 全部失败 → 降级（需求 3.7 / 3.9）
    if base is None or not base.has_any_data():
        if search_attempted:
            notes.append("检索片段兜底也未获得可信数字（无引用证据）")
        error = "；".join(notes) or "所有财务数据源均不可用"
        logger.error("financials degraded | ticker={} err={}", ticker, error[:200])
        return FinancialAnalysis(
            ticker=ticker.upper(),
            data_source="none",
            degraded=True,
            error=error,
            notes=notes,
        )

    # 5) LLM 生成解读（trends / summary），数据本身不受影响
    interpretation = _interpret(base)

    elapsed = time.perf_counter() - started
    m.tool_calls_total.labels(tool="financial_analysis", status="success").inc()
    logger.info(
        "financials collected | ticker={} source={} quote={} years={} revenue={} pe={} elapsed={:.1f}s",
        ticker.upper(), base.data_source, base.quote_source, base.fiscal_years,
        (base.income_statement.get(base.fiscal_years[0], {}) if base.fiscal_years else {}).get("revenue"),
        base.key_metrics.get("pe_ratio"), elapsed,
    )
    merged = base.model_dump()
    merged["notes"] = [*base.notes, *notes]     # base 已含 notes，必须合并而不是重复传参
    merged["trends"] = interpretation.trends
    merged["summary"] = interpretation.summary
    return FinancialAnalysis(**merged)
