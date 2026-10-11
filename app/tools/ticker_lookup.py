"""股票代码查询（需求 3.4 Agent 1 工具 TickerLookupTool）。

策略（需求 3.9：主工具失败 → 降级）：
    1) 本地映射表：覆盖 26 家常见公司，含中英文别名（含 "苹果"、"腾讯" 等）
    2) 未命中 → LLM 兜底判断（DeepSeek，直连 chat/completions，不依赖 CrewAI）
    3) LLM 也不可用 → 返回未命中（source=none），由上层决定标记需人工介入
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import httpx
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from app.config import settings
from app.core import metrics as m
from app.core.logging import logger

# (ticker, 规范公司名, 别名列表)
_COMPANIES: list[tuple[str, str, list[str]]] = [
    ("AAPL", "Apple Inc.", ["apple", "apple inc", "apple computer", "苹果", "苹果公司"]),
    ("MSFT", "Microsoft Corporation", ["microsoft", "microsoft corp", "微软", "微软公司"]),
    ("GOOGL", "Alphabet Inc.", ["alphabet", "google", "alphabet inc", "谷歌", "谷歌公司"]),
    ("AMZN", "Amazon.com, Inc.", ["amazon", "amazon.com", "亚马逊"]),
    ("NVDA", "NVIDIA Corporation", ["nvidia", "nvidia corp", "英伟达"]),
    ("TSLA", "Tesla, Inc.", ["tesla", "tesla motors", "特斯拉"]),
    ("META", "Meta Platforms, Inc.", ["meta", "meta platforms", "facebook", "脸书", "元宇宙平台"]),
    ("NFLX", "Netflix, Inc.", ["netflix", "奈飞", "网飞"]),
    ("AMD", "Advanced Micro Devices, Inc.", ["amd", "advanced micro devices", "超威半导体"]),
    ("INTC", "Intel Corporation", ["intel", "intel corp", "英特尔"]),
    ("IBM", "International Business Machines Corporation", ["ibm", "international business machines", "国际商业机器"]),
    ("ORCL", "Oracle Corporation", ["oracle", "oracle corp", "甲骨文"]),
    ("CRM", "Salesforce, Inc.", ["salesforce", "赛富时"]),
    ("ADBE", "Adobe Inc.", ["adobe", "adobe inc", "奥多比"]),
    ("AVGO", "Broadcom Inc.", ["broadcom", "博通"]),
    ("TSM", "Taiwan Semiconductor Manufacturing Company", ["tsmc", "taiwan semiconductor", "台积电"]),
    ("JPM", "JPMorgan Chase & Co.", ["jpmorgan", "jp morgan", "jpmorgan chase", "摩根大通"]),
    ("BAC", "Bank of America Corporation", ["bank of america", "bofa", "美国银行"]),
    ("V", "Visa Inc.", ["visa", "visa inc", "维萨"]),
    ("JNJ", "Johnson & Johnson", ["johnson & johnson", "johnson and johnson", "强生"]),
    ("WMT", "Walmart Inc.", ["walmart", "wal-mart", "沃尔玛"]),
    ("KO", "The Coca-Cola Company", ["coca-cola", "coca cola", "coke", "可口可乐"]),
    ("DIS", "The Walt Disney Company", ["disney", "walt disney", "迪士尼"]),
    ("BA", "The Boeing Company", ["boeing", "波音"]),
    ("PFE", "Pfizer Inc.", ["pfizer", "辉瑞"]),
    ("XOM", "Exxon Mobil Corporation", ["exxon", "exxon mobil", "埃克森美孚"]),
    ("BABA", "Alibaba Group Holding Limited", ["alibaba", "alibaba group", "阿里巴巴"]),
    ("TCEHY", "Tencent Holdings Limited", ["tencent", "tencent holdings", "腾讯"]),
]

# 归一化时剥离的公司后缀
_SUFFIXES = (
    "incorporated", "inc", "corporation", "corp", "company", "co", "limited", "ltd",
    "plc", "holdings", "holding", "group", "technologies", "technology",
    "公司", "集团", "股份有限公司", "有限公司",
)


def normalize(name: str) -> str:
    """归一化公司名：小写、去标点、去公司后缀、压缩空格。"""
    text = (name or "").lower().strip()
    text = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", text)   # 去标点（保留中英文与数字）
    text = re.sub(r"\s+", " ", text).strip()
    for suffix in _SUFFIXES:
        if text.endswith(" " + suffix):
            text = text[: -(len(suffix) + 1)].strip()
    return text


_ALIAS_INDEX: dict[str, tuple[str, str]] = {}
for _ticker, _canonical, _aliases in _COMPANIES:
    _ALIAS_INDEX[normalize(_canonical)] = (_ticker, _canonical)
    for _alias in _aliases:
        _ALIAS_INDEX.setdefault(normalize(_alias), (_ticker, _canonical))


@dataclass
class TickerResult:
    ticker: str | None
    canonical_name: str | None
    source: str          # local / llm / none
    confidence: float    # 1.0 本地精确命中；0.6 LLM 判断；0 未命中
    error: str | None = None


def lookup_local(company_name: str) -> TickerResult | None:
    """本地映射表查询（精确 + 前缀兜底）。"""
    key = normalize(company_name)
    if not key:
        return None
    if key in _ALIAS_INDEX:
        ticker, canonical = _ALIAS_INDEX[key]
        return TickerResult(ticker, canonical, "local", 1.0)

    # 前缀兜底："apple incorporated cupertino" -> apple
    for alias, (ticker, canonical) in _ALIAS_INDEX.items():
        if alias and (key.startswith(alias + " ") or alias.startswith(key + " ")):
            return TickerResult(ticker, canonical, "local", 0.9)
    return None


def _parse_json_object(text: str) -> dict:
    """容错解析：剥离 ```json 围栏与前后文字，取第一个 JSON 对象。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        raise ValueError(f"返回内容中不含 JSON：{(text or '')[:120]!r}")
    return json.loads(match.group(0))


def _llm_lookup(company_name: str) -> TickerResult:
    """LLM 兜底判断（DeepSeek）。失败返回 source=none。"""
    if not settings.llm_ready:
        return TickerResult(None, None, "none", 0.0, "LLM 未配置（DEEPSEEK_API_KEY 缺失）")

    prompt = (
        f"请判断公司「{company_name}」在美股的股票代码（ticker）。\n"
        "只返回 JSON，不要任何解释：{\"ticker\": \"XXXX\", \"confidence\": 0.0-1.0}\n"
        "若该公司不存在、未上市或你不确定，返回 {\"ticker\": null, \"confidence\": 0}"
    )
    started = time.perf_counter()
    try:
        resp = httpx.post(
            f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.deepseek_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                # 注意：实测 max_tokens 过小（如 64）时，思考类模型会把预算耗尽在推理上、
                # 返回空 content（finish_reason=length），故此处留足余量。
                "max_tokens": 512,
                "response_format": {"type": "json_object"},
            },
            timeout=settings.llm_timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        choice = payload["choices"][0]
        content = choice["message"].get("content") or ""
        if not content.strip():
            raise ValueError(
                f"模型返回空内容（finish_reason={choice.get('finish_reason')}，可能是思考 token 耗尽预算）"
            )
        data = _parse_json_object(content)
        ticker = (data.get("ticker") or "").strip().upper() or None
        confidence = float(data.get("confidence") or 0.0)
    except Exception as exc:
        elapsed = time.perf_counter() - started
        m.tool_calls_total.labels(tool="ticker_lookup", status="failed").inc()
        m.tool_duration_seconds.labels(tool="ticker_lookup").observe(elapsed)
        logger.warning("ticker LLM fallback failed | company={} err={}", company_name, exc)
        return TickerResult(None, None, "none", 0.0, f"{type(exc).__name__}: {exc}")

    elapsed = time.perf_counter() - started
    m.tool_calls_total.labels(tool="ticker_lookup", status="success").inc()
    m.tool_duration_seconds.labels(tool="ticker_lookup").observe(elapsed)
    return TickerResult(ticker, None, "llm", confidence)


def lookup_ticker(company_name: str, use_llm: bool = True) -> TickerResult:
    """主入口：本地表 → LLM → 未命中。"""
    hit = lookup_local(company_name)
    if hit:
        m.tool_calls_total.labels(tool="ticker_lookup", status="success").inc()
        logger.info(
            "ticker resolved | company={} ticker={} source=local", company_name, hit.ticker
        )
        return hit

    if use_llm:
        m.tool_fallbacks_total.labels(tool="ticker_table", fallback_tool="llm").inc()
        result = _llm_lookup(company_name)
        if result.ticker:
            logger.info(
                "ticker resolved | company={} ticker={} source=llm confidence={}",
                company_name, result.ticker, result.confidence,
            )
        return result

    m.tool_calls_total.labels(tool="ticker_lookup", status="failed").inc()
    return TickerResult(None, None, "none", 0.0, "本地表未命中且未启用 LLM 兜底")


class _TickerInput(BaseModel):
    company_name: str = Field(..., description="公司名称，如 Apple Inc.")


class TickerLookupTool(BaseTool):
    name: str = "ticker_lookup"
    description: str = (
        "根据公司名称查找美股股票代码（ticker），例如 Apple Inc. → AAPL。"
        "优先查本地映射表，未命中时用 LLM 判断。返回 JSON：{ticker, source, confidence}。"
    )
    args_schema: type[BaseModel] = _TickerInput

    def _run(self, company_name: str) -> str:
        r = lookup_ticker(company_name)
        return json.dumps(
            {
                "ticker": r.ticker,
                "canonical_name": r.canonical_name,
                "source": r.source,
                "confidence": r.confidence,
                "error": r.error,
            },
            ensure_ascii=False,
        )
