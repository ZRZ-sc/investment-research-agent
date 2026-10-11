"""SEC EDGAR XBRL 结构化财务数据（需求 3.9 降级：Yahoo 不可用时改用 SEC）。

⚠️ 本模块**只调用 SEC 的结构化 JSON 接口**，不下载 10-K 原文、不使用 MinerU：
    - ticker → CIK：https://www.sec.gov/files/company_tickers.json（进程内缓存）
    - 科目取值：  https://data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/<Tag>.json
   需求 3.3 的「下载 10-K PDF + MinerU 解析」仍属后续阶段，本阶段不做。

为什么需要它：本机出口 IP 被 Yahoo 持续限流（yfinance 返回空表），而 SEC 接口可达，
    正好对应需求 3.9 降级矩阵里 SEC ↔ Yahoo 互为备用数据源的约定。

合规要点（SEC 强制）：User-Agent 必须含联系方式（settings.sec_user_agent），
    请求频率上限 10 req/s（本地按 settings.sec_request_interval 串行节流）。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime
from typing import Any

import httpx
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from app.config import settings
from app.core import metrics as m
from app.core.logging import logger
from app.tools.yahoo_finance import (
    FinancialMetrics,
    TickerNoData,
    apply_pe,
    compute_ratios,
    fetch_quote,
)

# 科目 Tag 优先级（同名科目在不同公司/年度使用不同 Tag，按序取第一个有数据的）
_INCOME_TAGS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
}
_BALANCE_TAGS = {
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "stockholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
}
# 有息负债 = 长期债务的当期部分 + 非当期部分（不同 Tag 是「细分」而非「同义」，
# 必须相加；早前把 LongTermDebt 与 LongTermDebtNoncurrent 一起相加会重复计算）
_DEBT_TAGS = ["LongTermDebtCurrent", "LongTermDebtNoncurrent"]
_CASHFLOW_TAGS = {
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
}

_cik_cache: dict[str, str] = {}
_tickers_loaded = False
_ticker_map: dict[str, str] = {}
_client: httpx.Client | None = None

# 全局节流（跨线程）：SEC 上限 10 req/s，必须按**整进程**算速率。
# 实测：3 个任务并发 × 每个 4 线程抓取 ≈ 20+ req/s，SEC 会改用 404/空数据回应（而非 429），
# 表现为「SEC 未返回可用年报数据」，实为被限流（可口可乐在评测中就是这样被误判的）。
_SEC_MIN_INTERVAL = 0.13
_rate_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    """进程级最小请求间隔，保证并发任务合计速率不超 SEC 限额。"""
    global _last_request_at
    with _rate_lock:
        wait = _SEC_MIN_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


class SecUnavailable(RuntimeError):
    """SEC 接口不可用（网络/限流/解析异常）。"""


def _headers() -> dict[str, str]:
    return {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


def _get_client() -> httpx.Client:
    """复用同一 Client（连接池）：逐个 httpx.get 会重复 TLS 握手，
    实测 13 个科目要 47s，复用后降到数秒级。"""
    global _client
    if _client is None:
        _client = httpx.Client(
            headers=_headers(), timeout=settings.sec_timeout, follow_redirects=True
        )
    return _client


def _get(url: str) -> httpx.Response:
    """带全局节流与重试的 SEC 请求（遵守 10 req/s 上限）。"""
    last_error: Exception | None = None
    for attempt in range(1, settings.sec_max_retries + 1):
        try:
            _throttle()
            resp = _get_client().get(url)
            if resp.status_code == 404:
                # SEC 超速时会以 404 / 无 units 回应；等一会再确认，避免把限流误判成「该公司无数据」
                time.sleep(1.5)
                _throttle()
                resp = _get_client().get(url)
                if resp.status_code == 404:
                    raise TickerNoData(f"SEC 无该科目数据：{url.rsplit('/', 1)[-1]}")
            resp.raise_for_status()
            return resp
        except TickerNoData:
            raise
        except Exception as exc:
            last_error = exc
            logger.warning("SEC 请求失败（第 {}/{} 次）| {} | {}", attempt, settings.sec_max_retries, url[-60:], exc)
            time.sleep(settings.sec_request_interval * 5)
    raise SecUnavailable(f"SEC 请求失败：{type(last_error).__name__}: {last_error}")


def resolve_cik(ticker: str) -> str:
    """ticker → 10 位 CIK（首次调用加载全量映射表，进程内缓存）。"""
    global _tickers_loaded, _ticker_map
    key = ticker.upper()
    if key in _cik_cache:
        return _cik_cache[key]
    if not _tickers_loaded:
        resp = _get("https://www.sec.gov/files/company_tickers.json")
        payload = resp.json()
        _ticker_map = {
            str(row.get("ticker", "")).upper(): str(row.get("cik_str", "")).zfill(10)
            for row in payload.values()
            if row.get("ticker")
        }
        _tickers_loaded = True
        logger.info("SEC ticker→CIK 映射表已加载：{} 条", len(_ticker_map))
    cik = _ticker_map.get(key)
    if not cik:
        raise TickerNoData(f"SEC 映射表中没有该 ticker：{ticker}（可能非美股上市公司）")
    _cik_cache[key] = cik
    return cik


def _annual_points(rows: list[dict[str, Any]], duration: bool) -> dict[str, tuple[float, str]]:
    """筛出年报口径的数据点：{期末日期: (数值, 财年标签)}。

    - 只取 form=10-K 且 fp=FY 的数据点
    - 流量科目（利润表/现金流）额外要求 start→end 跨度 ≈ 1 年，
      否则会把 10-K 里混入的季度数据（如 Apple FY2018 Q3 的 53.2B）当成年度值
    - 财年标签用**期末年份**：SEC 的 fy 字段是「提交年份」，同一份 10-K 里
      2025/2024/2023 三个期间都会带 fy=2025，用它当标签会把三年揉成一年
    """
    out: dict[str, tuple[float, str]] = {}
    for row in rows:
        # 年报口径：10-K（美国本土）或 20-F（外国私人发行人，如阿里/中概 ADR）
        if row.get("form") not in ("10-K", "20-F", "10-K/A", "20-F/A"):
            continue
        if row.get("fp") not in (None, "", "FY"):
            continue
        value = row.get("val")
        end = row.get("end")
        if value is None or not end:
            continue
        if duration:
            start = row.get("start")
            if not start:
                continue
            try:
                span = (date.fromisoformat(end) - date.fromisoformat(start)).days
            except ValueError:
                continue
            if not 300 <= span <= 400:
                continue
        label = end[:4]
        out[end] = (float(value), label)     # 同一期末重复出现时保留最后一条
    return out


def _concept(cik: str, tag: str) -> dict[str, tuple[float, str]]:
    """（已废弃，保留兼容）按单科目请求；新路径改用 companyfacts，见 _load_facts。"""
    url = f"{settings.sec_edgar_base_url.rstrip('/')}/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"
    try:
        resp = _get(url)
    except TickerNoData:
        return {}
    units = (resp.json() or {}).get("units", {})
    # 取 USD 单位（EPS 为 USD/shares）
    unit_key = next((u for u in units if u.startswith("USD")), None)
    if not unit_key:
        return {}
    rows = units.get(unit_key, [])
    duration = bool(rows and rows[0].get("start"))
    return _annual_points(rows, duration=duration)


def _load_facts(cik: str) -> dict[str, Any]:
    """一次取回该公司**全部** us-gaap XBRL 事实（companyfacts，单请求）。

    为什么不再逐个科目请求 companyconcept：
      1) 它对部分 CIK 不可靠——实测可口可乐（CIK 21344）的 companyconcept 对 Assets 等科目
         返回 200 但没有任何数据点，而 companyfacts 里 Assets 有 144 个点（含 10-K）；
      2) 逐个科目要 14 次请求，3 个任务并发时速率超过 SEC 的 10 req/s 限额，
         SEC 会改用 404/空数据回应（不是 429），把「限流」伪装成「该公司没有数据」。
    单请求拿全后，请求压力下降一个数量级。
    """
    url = f"{settings.sec_edgar_base_url.rstrip('/')}/api/xbrl/companyfacts/CIK{cik}.json"
    resp = _get(url)
    facts = (resp.json() or {}).get("facts", {})
    return facts.get("us-gaap", {}) or {}


def _tag_points(gaap: dict[str, Any], tag: str) -> dict[str, tuple[float, str]]:
    """从 companyfacts 里取某个 Tag 的年报口径数据点。"""
    node = gaap.get(tag)
    if not node:
        return {}
    units = node.get("units", {})
    unit_key = next((u for u in units if u.startswith("USD")), None)
    if not unit_key:
        return {}
    rows = units.get(unit_key, [])
    duration = bool(rows and rows[0].get("start"))
    return _annual_points(rows, duration=duration)


def _first_tag(gaap: dict[str, Any], tags: list[str]) -> dict[str, tuple[float, str]]:
    for tag in tags:
        values = _tag_points(gaap, tag)
        if values:
            return values
    return {}


def _sum_tags(gaap: dict[str, Any], tags: list[str]) -> dict[str, tuple[float, str]]:
    """多个 Tag 按期末相加（如长期债务的当期部分 + 非当期部分）。"""
    merged: dict[str, tuple[float, str]] = {}
    for tag in tags:
        for end, (value, label) in _tag_points(gaap, tag).items():
            current = merged.get(end, (0.0, label))[0]
            merged[end] = (current + value, label)
    return merged


def _extract_statements(
    gaap: dict[str, Any],
) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """从 companyfacts 抽取三大报表科目（{字段: {期末: (值, 标签)}}）。"""
    income = {field: _first_tag(gaap, tags) for field, tags in _INCOME_TAGS.items()}
    balance = {field: _first_tag(gaap, tags) for field, tags in _BALANCE_TAGS.items()}
    debt = _sum_tags(gaap, _DEBT_TAGS)
    if debt:
        balance["total_debt"] = debt
    cash_flow = {field: _first_tag(gaap, tags) for field, tags in _CASHFLOW_TAGS.items()}
    return (
        {k: v for k, v in income.items() if v},
        {k: v for k, v in balance.items() if v},
        {k: v for k, v in cash_flow.items() if v},
    )


def fetch_sec_financials(ticker: str, years: int = 3) -> FinancialMetrics:
    """SEC XBRL 路径：近 N 年年报数据 + 报价（新浪/腾讯）+ 指标计算。"""
    started = time.perf_counter()
    try:
        cik = resolve_cik(ticker)
        gaap = _load_facts(cik)
        if not gaap:
            raise TickerNoData(f"SEC 未返回该公司的 XBRL 数据：{ticker}")
        income_raw, balance_raw, cash_raw = _extract_statements(gaap)
    except TickerNoData:
        raise
    except Exception as exc:
        m.tool_calls_total.labels(tool="sec_financials", status="failed").inc()
        raise SecUnavailable(f"{type(exc).__name__}: {exc}") from exc

    # 以营收的期末日期为主轴取最近 N 个财年
    anchor = income_raw.get("revenue") or balance_raw.get("total_assets") or {}
    if not anchor:
        m.tool_calls_total.labels(tool="sec_financials", status="failed").inc()
        raise TickerNoData(f"SEC 未返回可用年报数据：{ticker}")

    periods = sorted(anchor.keys(), reverse=True)[:years]
    year_labels = [anchor[p][1] for p in periods]

    def build(raw: dict[str, dict[str, tuple[float, str]]]) -> dict[str, dict[str, float | None]]:
        out: dict[str, dict[str, float | None]] = {}
        for period, label in zip(periods, year_labels):
            out[label] = {}
            for field, values in raw.items():
                hit = values.get(period)
                out[label][field] = hit[0] if hit else None
        return out

    income = build(income_raw)
    balance = build(balance_raw)
    cash_flow = build(cash_raw)

    # 自由现金流 = 经营现金流 - 资本开支。注意 XBRL 的 PaymentsToAcquirePropertyPlantAndEquipment
    # 是正数（yfinance 的同名行则是负数），统一取绝对值相减，避免符号口径差异算错
    for label, values in cash_flow.items():
        ocf = values.get("operating_cash_flow")
        capex = values.get("capex")
        values["free_cash_flow"] = (
            ocf - abs(capex) if (ocf is not None and capex is not None) else None
        )

    quote = fetch_quote(ticker)
    market_cap = quote.get("market_cap")
    metrics = compute_ratios(income, balance, market_cap, year_labels)
    apply_pe(metrics, quote.get("pe_ratio"))

    m.tool_calls_total.labels(tool="sec_financials", status="success").inc()
    m.tool_duration_seconds.labels(tool="sec_financials").observe(time.perf_counter() - started)
    logger.info(
        "SEC XBRL ok | ticker={} cik={} 财年={} 营收={} 耗时={:.1f}s",
        ticker.upper(), cik, year_labels,
        (income.get(year_labels[0], {}) if year_labels else {}).get("revenue"),
        time.perf_counter() - started,
    )
    return FinancialMetrics(
        ticker=ticker.upper(),
        currency="USD",
        fiscal_years=year_labels,
        income_statement=income,
        balance_sheet=balance,
        cash_flow=cash_flow,
        key_metrics=metrics,
        market_cap=market_cap,
        stock_price=quote.get("stock_price"),
        data_source="sec_xbrl",
        quote_source=quote.get("quote_source"),
        notes=[f"数据来自 SEC EDGAR XBRL（CIK {cik}）；报价来自 {quote.get('quote_source')}"],
    )


class _SecInput(BaseModel):
    ticker: str = Field(..., description="美股股票代码，如 AAPL")


class SecFinancialsTool(BaseTool):
    name: str = "sec_financials"
    description: str = (
        "从 SEC EDGAR 的 XBRL 结构化接口获取美股上市公司近 3 年年报财务数据"
        "（营收/净利润/毛利/经营现金流/资产/负债/股东权益等）。"
        "仅结构化 JSON，不下载 10-K 原文。返回 JSON。"
    )
    args_schema: type[BaseModel] = _SecInput

    def _run(self, ticker: str) -> str:
        try:
            data = fetch_sec_financials(ticker)
        except Exception as exc:
            return json.dumps(
                {"error": f"{type(exc).__name__}: {exc}", "ticker": ticker}, ensure_ascii=False
            )
        return data.model_dump_json()
