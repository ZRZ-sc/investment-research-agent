"""信息搜集 Agent（需求 3.4 Agent 1：信息搜集专家 / 3.3 Task 1）。

流水线：
    1) ticker 预解析   lookup_ticker()（本地 26 家映射表 → LLM 兜底），作为线索注入任务
    2) 资料预取        gather_company_context()（Wikipedia → DuckDuckGo → Bing 降级链）
    3) CrewAI 执行      Agent（role=信息搜集专家，LLM=DeepSeek Flash）+ 4 个工具
    4) 输出校验         Pydantic CompanyProfile；失败最多重试 settings.step_max_retries 次（需求 3.7）
    5) 仍失败 → 降级    degraded=True + error，上层记录并跳过该步继续（需求 3.7 / 3.9）

说明：预取资料会一并写入任务描述，避免完全依赖 LLM「主动调用工具」的自觉性；
     LLM 仍可自行调用 duckduckgo_search / bing_search / wikipedia_search / ticker_lookup 补充。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from crewai import Agent, Crew, LLM, Process, Task
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import settings
from app.core import metrics as m
from app.core.logging import logger
from app.tools.ticker_lookup import TickerLookupTool, lookup_ticker
from app.tools.web_search import (
    BingSearchTool,
    DuckDuckGoSearchTool,
    WikipediaSearchTool,
    gather_company_context,
)

# 关闭 CrewAI 执行追踪：默认首次运行会在 TTY 下弹「Share this execution trace?」并阻塞 20s，
# Celery worker / 容器里必须保持完全非交互。
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

AGENT_NAME = "InformationSpecialist"
STUB_STEP = "profile"

_JSON_SCHEMA_HINT = """{
  "company_name": "公司全称",
  "ticker": "美股股票代码（无法确定填 null）",
  "description": "150-300 字公司简介：主营业务、成立背景、市场地位",
  "sector": "所属行业，如 Technology / Consumer Electronics",
  "products": ["主要产品或服务", "..."],
  "competitors": ["主要竞争对手", "..."],
  "recent_news": [
    {"date": "2025-08-01", "title": "新闻标题", "summary": "一句话摘要", "url": "来源链接"}
  ],
  "market_cap": "市值（不知道填 null）",
  "stock_price": "股价（不知道填 null）",
  "unknown_company": false
}"""


# ---------------------------------------------------------------------------
# 输出模型（需求 3.7：Pydantic 校验）
# ---------------------------------------------------------------------------
class NewsItem(BaseModel):
    date: str | None = None
    title: str = ""
    summary: str = ""
    url: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_str(cls, value: Any) -> Any:
        """容忍 LLM 把一条新闻写成纯字符串。"""
        return {"title": value} if isinstance(value, str) else value


class CompanyProfile(BaseModel):
    company_name: str = Field(..., min_length=1)
    ticker: str | None = None
    description: str = ""
    sector: str | None = None
    products: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    recent_news: list[NewsItem] = Field(default_factory=list)
    market_cap: str | None = None
    stock_price: str | None = None
    # ---- 运行元信息（非 LLM 输出，由代码补齐，便于审计 F5）----
    data_sources: list[str] = Field(default_factory=list)
    unknown_company: bool = False
    degraded: bool = False
    error: str | None = None

    @field_validator("products", "competitors", mode="before")
    @classmethod
    def _to_list(cls, value: Any) -> Any:
        """容忍逗号分隔字符串或 null。"""
        if value is None:
            return []
        if isinstance(value, str):
            return [p.strip() for p in re.split(r"[,，;；、]", value) if p.strip()]
        return value

    @field_validator("sector", "market_cap", "stock_price", "ticker", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _require_content(self) -> "CompanyProfile":
        """需求 3.7：真正触发重试的校验 —— 非未知公司必须有实质简介。"""
        if not self.unknown_company and not self.description.strip():
            raise ValueError("description 为空：未知公司请显式返回 unknown_company=true")
        return self


# ---------------------------------------------------------------------------
# LLM 与 Agent 构建
# ---------------------------------------------------------------------------
def build_llm(tier: str = "flash") -> LLM:
    """DeepSeek（OpenAI 兼容）LLM：tier=flash（默认档）/ pro（高质量档）。"""
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


def build_information_agent(llm: LLM | None = None) -> Agent:
    """信息搜集专家（需求 3.4 的角色设定）。"""
    return Agent(
        role="信息搜集专家",
        goal="为用户指定的公司搜集全面、准确的背景信息和市场动态，并确定股票代码",
        backstory=(
            "你是一位经验丰富的投资研究助理，擅长从互联网海量信息中快速提炼关键事实。"
            "你有 10 年金融信息检索经验，熟悉如何从新闻、百科、行业报告中筛选高价值信息。"
        ),
        llm=llm or build_llm("flash"),
        tools=[
            DuckDuckGoSearchTool(),
            WikipediaSearchTool(),
            BingSearchTool(),
            TickerLookupTool(),
        ],
        allow_delegation=False,
        max_iter=4,
        verbose=False,
    )


# ---------------------------------------------------------------------------
# JSON 提取（容忍 ```json 围栏与前后废话）
# ---------------------------------------------------------------------------
def extract_json(text: str) -> dict:
    if not text or not text.strip():
        raise ValueError("LLM 输出为空")

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    candidates: list[str] = [cleaned]
    start = cleaned.find("{")
    if start >= 0:
        depth, in_str, escaped = 0, False, False
        for idx in range(start, len(cleaned)):
            ch = cleaned[idx]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(cleaned[start : idx + 1])
                    break

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError(f"无法从输出中解析 JSON：{text[:200]!r}")


# ---------------------------------------------------------------------------
# 任务描述与执行
# ---------------------------------------------------------------------------
def _task_description(
    company_name: str,
    ticker_hint: str | None,
    ticker_source: str | None,
    context: str,
    previous_error: str | None = None,
) -> str:
    # 反「过度保守」提示：真实公司若因检索资料偏薄被判成 unknown_company，会白白降级。
    # 依据 ticker 的证据强度分两档：本地表/调用方给出的是硬证据；LLM 兜底解析的只作参考。
    if ticker_hint and ticker_source in ("local", "request", "sec_verified"):
        existence_hint = (
            f"（注：股票代码 {ticker_hint} 已确认有效（本地映射表／调用方／SEC 备案校验），"
            "该公司确实存在，不要仅因资料偏少就返回 unknown_company=true；缺的字段按第 5 条填 null 即可）"
        )
    elif ticker_hint:
        existence_hint = (
            f"（注：已初步解析出股票代码 {ticker_hint}，请据此核对检索资料；"
            "若资料确实与该公司无关，仍可返回 unknown_company=true）"
        )
    else:
        existence_hint = ""

    parts = [
        f"请为「{company_name}」建立公司档案，并且**只输出一个 JSON 对象**"
        "（不要 Markdown 代码块、不要任何解释文字）。",
        f"已解析到的股票代码线索：{ticker_hint or '未解析到，请调用 ticker_lookup 工具查询'}",
        "",
        "输出 JSON 结构：",
        _JSON_SCHEMA_HINT,
        "",
        "硬性要求：",
        "1. description / sector / products / competitors 必须依据下方资料或工具检索结果，禁止编造；",
        f"2. recent_news 给 3-5 条真实新闻，优先近两年，按时间倒序；",
        "3. ticker 优先采用上面的线索值，否则调用 ticker_lookup 工具确认；",
        "4. 先判断下方资料是否真的在讲这家公司：若资料为空、或与该公司无关"
        "（例如公司名不存在），必须返回 {\"company_name\": \"" + company_name +
        "\", \"unknown_company\": true, \"description\": \"\"}，不要虚构公司信息；" + existence_hint,
        "5. 所有字段都要出现；不确定的字符串字段填 null，列表字段填 []；",
        "6. recent_news 的 title 与 url 必须**直接来自检索结果**，禁止改写、拼接或编造；"
        "只有资料中明确写出日期时才填 date，否则填 null（绝不推测日期）；"
        "若检索结果里没有真正的新闻类内容，宁可少给几条，也不要拿官网产品页/支持页充当新闻。",
    ]
    if context:
        parts += ["", "===== 已检索资料（可作为事实依据，也可自行调用工具补充）=====", context]
    else:
        parts += [
            "",
            "（未预取到任何资料，请立即调用 bing_search / duckduckgo_search / wikipedia_search 检索；"
            "若检索仍无结果，按第 4 条返回 unknown_company=true）",
        ]
    if previous_error:
        parts += [
            "",
            f"⚠️ 上一次输出不合法（{previous_error}）。本次务必只返回严格合法的 JSON 对象。",
        ]
    return "\n".join(parts)


def _run_crew(
    company_name: str,
    ticker_hint: str | None,
    ticker_source: str | None,
    context: str,
    previous_error: str | None,
) -> str:
    """执行一次 CrewAI 流程，返回原始文本输出。"""
    agent = build_information_agent()
    task = Task(
        description=_task_description(company_name, ticker_hint, ticker_source, context, previous_error),
        expected_output="一个严格符合要求的 JSON 对象（无 Markdown 代码块、无解释文字）",
        agent=agent,
    )
    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
        tracing=False,  # 关闭执行追踪：避免交互式提示阻塞 worker（headless 环境）
    )

    started = time.perf_counter()
    result = crew.kickoff()
    elapsed = time.perf_counter() - started

    model = settings.llm_model("flash")
    m.llm_request_duration_seconds.labels(model=model, agent=AGENT_NAME).observe(elapsed)
    usage = getattr(result, "token_usage", None)
    if usage is not None:
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if prompt_tokens:
            m.llm_tokens_total.labels(model=model, type="prompt").inc(int(prompt_tokens))
        if completion_tokens:
            m.llm_tokens_total.labels(model=model, type="completion").inc(int(completion_tokens))

    raw = getattr(result, "raw", None) or str(result)
    logger.info(
        "info agent finished | company={} elapsed={:.1f}s tokens={}", company_name, elapsed, usage
    )
    return raw


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def collect_company_profile(company_name: str, ticker_hint: str | None = None) -> CompanyProfile:
    """信息搜集主入口（同步；在 Celery worker 线程中经 asyncio.to_thread 调用）。"""
    started = time.perf_counter()

    # 1) ticker：本地映射表 → LLM 兜底（同时记录来源，用于判断证据强度）
    ticker_source = "request" if ticker_hint else None
    if not ticker_hint:
        hit = lookup_ticker(company_name)
        ticker_hint, ticker_source = hit.ticker, (hit.source if hit.ticker else None)

    # LLM 兜底解析出的代码只算「软证据」，容易被误判为未知公司；
    # 若该代码在 SEC EDGAR 有备案，则升级为硬证据（映射表进程内缓存，代价≈一次请求）
    if ticker_hint and ticker_source == "llm":
        try:
            from app.tools.sec_financials import resolve_cik

            resolve_cik(ticker_hint)
            ticker_source = "sec_verified"
            logger.info("ticker 经 SEC 交叉验证 | company={} ticker={}", company_name, ticker_hint)
        except Exception as exc:
            logger.debug("SEC 交叉验证未通过 | ticker={} err={}", ticker_hint, exc)

    # 2) 资料预取（Wikipedia → DuckDuckGo → Bing）
    context, sources = gather_company_context(company_name, ticker_hint)

    # 3) 执行 + 校验（需求 3.7：失败最多重试 step_max_retries 次）
    attempts = max(1, settings.step_max_retries + 1)
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            raw = _run_crew(company_name, ticker_hint, ticker_source, context, last_error)
            profile = CompanyProfile.model_validate(extract_json(raw))
            profile.company_name = profile.company_name.strip() or company_name
            if not profile.ticker:
                profile.ticker = ticker_hint
            profile.data_sources = list(dict.fromkeys([*profile.data_sources, *sources]))

            if attempt > 1:
                m.research_step_retries_total.labels(step=STUB_STEP).inc()

            if profile.unknown_company:
                profile.degraded = True
                profile.error = f"检索未找到「{company_name}」的相关信息（LLM 判定为未知公司）"
                if not profile.description.strip():
                    # 给下游步骤（财报分析/报告撰写）一个可读的兜底描述，而不是空字符串
                    profile.description = (
                        f"未检索到「{company_name}」的公开资料，无法建立公司档案（已降级）。"
                    )
                logger.warning(
                    "company unknown | company={} sources={} elapsed={:.1f}s",
                    company_name, profile.data_sources, time.perf_counter() - started,
                )
            else:
                if len(profile.recent_news) < 3:
                    logger.warning(
                        "recent_news 少于 3 条 | company={} count={}",
                        company_name, len(profile.recent_news),
                    )
                logger.info(
                    "profile collected | company={} ticker={} news={} sources={} attempt={} elapsed={:.1f}s",
                    company_name, profile.ticker, len(profile.recent_news),
                    profile.data_sources, attempt, time.perf_counter() - started,
                )
            return profile

        except Exception as exc:  # JSON 解析失败 / Pydantic 校验失败 / LLM 调用异常
            last_error = f"{type(exc).__name__}: {exc}"
            m.agent_validation_failures_total.labels(agent=AGENT_NAME).inc()
            logger.warning(
                "profile attempt failed | company={} attempt={}/{} err={}",
                company_name, attempt, attempts, last_error[:200],
            )

    # 4) 降级：记录错误，返回兜底 profile（上层跳过该步并继续，需求 3.7 / 3.9）
    logger.error("profile degraded | company={} err={}", company_name, last_error)
    return CompanyProfile(
        company_name=company_name,
        ticker=ticker_hint,
        description=f"未能获取「{company_name}」的公司信息：已重试 {attempts - 1} 次仍失败，跳过信息搜集步骤。",
        data_sources=sources,
        degraded=True,
        error=last_error,
    )
