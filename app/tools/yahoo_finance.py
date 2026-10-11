"""Yahoo Finance 财务数据工具（需求 3.4 Agent 2 工具 / 3.9 降级矩阵）。

主数据源：yfinance —— 近 N 年三大报表（利润表 / 资产负债表 / 现金流量表）+ 当前股价与市值，
并在本模块内计算核心指标（等价需求 3.4 的 FinancialCalculatorTool）：
    PE、ROE、毛利率、净利率、营业利润率、营收增长（YoY / CAGR）、资产负债率、债务股本比。

降级设计（需求 3.9：主工具失败 → 切换备用数据源）：
    报表：yfinance →（限流/失败）→ app/tools/sec_financials.py 的 SEC XBRL 结构化接口
    报价：yfinance fast_info/info →（限流/失败）→ 新浪行情 → 腾讯行情（均免 Key、国内可达）

⚠️ 本机实测：Yahoo 对该出口 IP **持续限流**（`YFRateLimitError`，三大报表返回空表、
    info/fast_info 抛错），因此实际数据来自上述备用源；换到可正常访问 Yahoo 的网络后，
    yfinance 会自动恢复为主源，无需改动代码。连续失败会进入冷却（默认 300s），避免每单都白等。
"""
from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from app.core import metrics as m
from app.core.logging import logger

_T = TypeVar("_T")
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_FETCH_TIMEOUT = 45.0        # yfinance 抓取硬性时限（秒）
_QUOTE_TIMEOUT = 8.0         # 报价接口超时
_COOLDOWN_SECONDS = 300.0    # Yahoo 系统性失败后的冷却时长
_FAILURE_THRESHOLD = 2       # 连续失败多少次判定为「Yahoo 不可用」

_failures = 0
_cooldown_until = 0.0

# 报表行名候选（不同 yfinance 版本/公司命名有差异，用模糊匹配兜住）
_INCOME_ROWS = {
    "revenue": ["Total Revenue", "Operating Revenue"],
    "gross_profit": ["Gross Profit"],
    "operating_income": ["Operating Income", "EBIT"],
    "net_income": ["Net Income Common Stockholders", "Net Income"],
    "eps": ["Diluted EPS", "Basic EPS"],
}
_BALANCE_ROWS = {
    "total_assets": ["Total Assets"],
    "total_liabilities": ["Total Liabilities Net Minority Interest", "Total Liabilities"],
    "stockholders_equity": ["Stockholders Equity", "Total Stockholder Equity"],
    "total_debt": ["Total Debt"],
    "cash": ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
}
_CASHFLOW_ROWS = {
    "operating_cash_flow": ["Operating Cash Flow", "Total Cash From Operating Activities"],
    "free_cash_flow": ["Free Cash Flow"],
    "capex": ["Capital Expenditure"],
}


class YahooUnavailable(RuntimeError):
    """Yahoo 系统性不可用（限流 / 超时 / 网络异常），会累计并触发冷却。"""


class TickerNoData(RuntimeError):
    """该 ticker 本身无数据（未上市 / 代码错误），不触发冷却。"""


# ---------------------------------------------------------------------------
# 数据模型（SEC 降级源复用同一结构，见 app/tools/sec_financials.py）
# ---------------------------------------------------------------------------
class FinancialMetrics(BaseModel):
    ticker: str
    currency: str | None = None
    fiscal_years: list[str] = Field(default_factory=list)
    income_statement: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    balance_sheet: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    cash_flow: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    key_metrics: dict[str, float | None] = Field(default_factory=dict)
    market_cap: float | None = None
    stock_price: float | None = None
    # ---- 溯源与状态 ----
    data_source: str = "yfinance"        # yfinance / sec_xbrl / bing / none
    quote_source: str | None = None      # yfinance / sina / tencent
    degraded: bool = False
    error: str | None = None
    notes: list[str] = Field(default_factory=list)

    def has_statements(self) -> bool:
        return any(
            (self.income_statement.get(y) or {}).get("revenue")
            for y in self.fiscal_years
        )

    def has_any_data(self) -> bool:
        """是否拿到了任何可用数据（报表 / 关键指标 / 报价任一即可）。"""
        return bool(
            self.has_statements()
            or any(v is not None for v in self.key_metrics.values())
            or self.market_cap is not None
            or self.stock_price is not None
        )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _num(value: Any) -> float | None:
    """转 float；NaN / Inf / 空值一律返回 None。"""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _with_timeout(fn: Callable[..., _T], seconds: float, *args: Any) -> _T:
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        return executor.submit(fn, *args).result(timeout=seconds)
    except TimeoutError as exc:
        raise YahooUnavailable(f"抓取超过 {seconds}s 硬性时限") from exc
    finally:
        executor.shutdown(wait=False)


def is_cooling() -> bool:
    """Yahoo 是否处于冷却期（冷却期内直接跳过，交给备用源）。"""
    return time.time() < _cooldown_until


def _register_failure(reason: str) -> None:
    global _failures, _cooldown_until
    _failures += 1
    if _failures >= _FAILURE_THRESHOLD and not is_cooling():
        _cooldown_until = time.time() + _COOLDOWN_SECONDS
        logger.warning(
            "yahoo finance unavailable, cooling down {}s | reason={}",
            int(_COOLDOWN_SECONDS), reason[:140],
        )


def _register_success() -> None:
    global _failures
    _failures = 0


def reset_state() -> None:
    """清空失败计数与冷却（测试 / 排障用）。"""
    global _failures, _cooldown_until
    _failures = 0
    _cooldown_until = 0.0


# ---------------------------------------------------------------------------
# 报表解析
# ---------------------------------------------------------------------------
def _match_row(frame: Any, candidates: list[str]) -> Any | None:
    if frame is None or getattr(frame, "empty", True):
        return None
    index = list(frame.index)
    normalized = {str(name).strip().lower(): name for name in index}
    for candidate in candidates:
        key = candidate.strip().lower()
        if key in normalized:
            return frame.loc[normalized[key]]
    for candidate in candidates:
        key = candidate.strip().lower()
        for name in index:
            if key in str(name).strip().lower():
                return frame.loc[name]
    return None


def _extract(frame: Any, row_map: dict[str, list[str]], years: list[Any]) -> dict[str, dict[str, float | None]]:
    """{年份: {字段: 数值}}"""
    out: dict[str, dict[str, float | None]] = {}
    for column in years:
        label = str(column)[:4] if str(column)[:4].isdigit() else str(column)
        out[label] = {}
    for field, candidates in row_map.items():
        row = _match_row(frame, candidates)
        if row is None:
            continue
        for column in years:
            label = str(column)[:4] if str(column)[:4].isdigit() else str(column)
            out[label][field] = _num(row.get(column))
    return out


def compute_ratios(
    income: dict[str, dict[str, float | None]],
    balance: dict[str, dict[str, float | None]],
    market_cap: float | None,
    years: list[str],
) -> dict[str, float | None]:
    """计算核心指标；任一分母缺失即返回 None（不猜、不编）。"""
    metrics: dict[str, float | None] = {
        "pe_ratio": None,
        "pe_ratio_fiscal": None,
        "roe": None,
        "gross_margin": None,
        "net_margin": None,
        "operating_margin": None,
        "revenue_growth_yoy": None,
        "revenue_growth_3y": None,
        "debt_to_equity": None,
        "liabilities_to_equity": None,
        "liabilities_to_assets": None,
    }
    if not years:
        return metrics

    latest = years[0]
    inc = income.get(latest, {})
    bal = balance.get(latest, {})
    revenue = inc.get("revenue")
    net_income = inc.get("net_income")
    equity = bal.get("stockholders_equity")

    if revenue:
        if inc.get("gross_profit") is not None:
            metrics["gross_margin"] = round(inc["gross_profit"] / revenue, 4)
        if net_income is not None:
            metrics["net_margin"] = round(net_income / revenue, 4)
        if inc.get("operating_income") is not None:
            metrics["operating_margin"] = round(inc["operating_income"] / revenue, 4)
    if equity and net_income is not None:
        metrics["roe"] = round(net_income / equity, 4)
    if market_cap and net_income:
        # 财年口径：市值 / 最新财年净利润。行情源的 TTM 口径 PE 由 apply_pe() 优先覆盖
        metrics["pe_ratio_fiscal"] = round(market_cap / net_income, 2)
    if equity:
        if bal.get("total_debt") is not None:
            metrics["debt_to_equity"] = round(bal["total_debt"] / equity, 2)
        if bal.get("total_liabilities") is not None:
            metrics["liabilities_to_equity"] = round(bal["total_liabilities"] / equity, 2)
    if bal.get("total_assets") and bal.get("total_liabilities") is not None:
        metrics["liabilities_to_assets"] = round(
            bal["total_liabilities"] / bal["total_assets"], 4
        )

    # 营收增长：同比 + 区间 CAGR
    revenues = [(y, (income.get(y) or {}).get("revenue")) for y in years]
    revenues = [(y, v) for y, v in revenues if v]
    if len(revenues) >= 2:
        latest_rev, prev_rev = revenues[0][1], revenues[1][1]
        if prev_rev:
            metrics["revenue_growth_yoy"] = round(latest_rev / prev_rev - 1, 4)
        oldest_rev = revenues[-1][1]
        span = len(revenues) - 1
        if oldest_rev and oldest_rev > 0 and span > 0:
            metrics["revenue_growth_3y"] = round((latest_rev / oldest_rev) ** (1 / span) - 1, 4)
    return metrics


# ---------------------------------------------------------------------------
# 报价：yfinance → 新浪 → 腾讯
# ---------------------------------------------------------------------------
def _quote_yfinance(ticker: str) -> dict[str, Any]:
    import yfinance as yf

    tk = yf.Ticker(ticker)
    price = market_cap = currency = pe = None
    try:
        info = tk.info or {}
        price = _num(info.get("currentPrice") or info.get("regularMarketPrice"))
        market_cap = _num(info.get("marketCap"))
        currency = info.get("currency")
        pe = _num(info.get("trailingPE"))
    except Exception as exc:  # 限流/字段缺失时退到 fast_info
        logger.debug("yfinance info 不可用（{}），尝试 fast_info", type(exc).__name__)
    if price is None or market_cap is None:
        fast = tk.fast_info
        price = price if price is not None else _num(fast.get("lastPrice"))
        market_cap = market_cap if market_cap is not None else _num(fast.get("marketCap"))
        currency = currency or fast.get("currency")
    if price is None and market_cap is None:
        raise YahooUnavailable("yfinance 未返回报价")
    return {
        "stock_price": price,
        "market_cap": market_cap,
        "pe_ratio": pe,
        "currency": currency,
        "quote_source": "yfinance",
    }


def _quote_sina(ticker: str) -> dict[str, Any]:
    import httpx

    resp = httpx.get(
        f"https://hq.sinajs.cn/list=gb_{ticker.lower()}",
        headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn"},
        timeout=_QUOTE_TIMEOUT,
    )
    resp.raise_for_status()
    resp.encoding = "gbk"
    body = resp.text.split('="', 1)[-1].rstrip('";\n')
    parts = body.split(",")
    if len(parts) < 15 or not parts[1]:
        raise ValueError(f"新浪返回格式异常：{body[:80]}")
    return {
        "stock_price": _num(parts[1]),
        "market_cap": _num(parts[12]),   # 总市值（美元）
        "pe_ratio": _num(parts[14]),     # 市盈率
        "currency": "USD",
        "quote_source": "sina",
        "name": parts[0],
        "eps": _num(parts[13]),
    }


def _quote_tencent(ticker: str) -> dict[str, Any]:
    import httpx

    resp = httpx.get(
        f"https://qt.gtimg.cn/q=us{ticker.upper()}",
        headers={"User-Agent": _UA},
        timeout=_QUOTE_TIMEOUT,
    )
    resp.raise_for_status()
    resp.encoding = "gbk"
    raw = resp.text.split('="', 1)[-1].rstrip('";\n')
    parts = raw.split("~")
    if len(parts) < 46 or not parts[3]:
        raise ValueError(f"腾讯返回格式异常：{raw[:80]}")
    market_cap = _num(parts[44])
    return {
        "stock_price": _num(parts[3]),
        "market_cap": market_cap * 1e8 if market_cap else None,   # 单位：亿美元
        "pe_ratio": None,   # 腾讯美股字段口径不稳定，PE 只用新浪/yfinance
        "currency": parts[35] or "USD",
        "quote_source": "tencent",
        "name": parts[46] if len(parts) > 46 else None,
    }


def _sane_quote(quote: dict[str, Any], require_market_cap: bool = False) -> dict[str, Any]:
    """过滤「占位/无效」报价，无效时抛错。

    实测：新浪对不存在的 ticker（如 ZZZZ）会返回一行占位数据
    `ZZZZ,1.0000,0.00,...,0,0,0,0.00,--`（价格 1.0000、市值 0、PE 0），
    若不校验就会被当成真实行情，导致「无数据公司」被判成有数据。
    行情源（新浪/腾讯）对真实标的必然给出市值，故对它们额外要求市值有效。
    """
    price = quote.get("stock_price")
    if price is not None and price <= 0:
        quote["stock_price"] = None
    market_cap = quote.get("market_cap")
    if market_cap is not None and market_cap <= 0:
        quote["market_cap"] = None
    pe = quote.get("pe_ratio")
    if pe is not None and pe <= 0:
        quote["pe_ratio"] = None
    if quote.get("stock_price") is None and quote.get("market_cap") is None:
        raise ValueError("报价接口返回占位/无效数据（价格与市值均无效）")
    if require_market_cap and quote.get("market_cap") is None:
        raise ValueError("报价接口未返回有效市值（疑似占位数据）")
    return quote


def fetch_quote(ticker: str) -> dict[str, Any]:
    """取当前股价 / 市值 / PE，按 yfinance → 新浪 → 腾讯 降级。"""
    errors: dict[str, str] = {}
    chain: list[tuple[str, Callable[[str], dict[str, Any]]]] = []
    if not is_cooling():
        chain.append(("yfinance", _quote_yfinance))
    chain += [("sina", _quote_sina), ("tencent", _quote_tencent)]

    for idx, (name, fetcher) in enumerate(chain):
        started = time.perf_counter()
        try:
            quote = _sane_quote(fetcher(ticker), require_market_cap=name in ("sina", "tencent"))
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
            m.tool_calls_total.labels(tool=f"quote:{name}", status="failed").inc()
            m.tool_duration_seconds.labels(tool=f"quote:{name}").observe(time.perf_counter() - started)
            continue
        m.tool_calls_total.labels(tool=f"quote:{name}", status="success").inc()
        m.tool_duration_seconds.labels(tool=f"quote:{name}").observe(time.perf_counter() - started)
        if idx > 0:
            m.tool_fallbacks_total.labels(tool="quote:yfinance", fallback_tool=name).inc()
            logger.info("quote fallback | yfinance -> {} | ticker={}", name, ticker)
        quote["errors"] = errors
        return quote

    m.tool_calls_total.labels(tool="quote:all", status="failed").inc()
    return {"errors": errors, "quote_source": None}


# ---------------------------------------------------------------------------
# yfinance 报表抓取
# ---------------------------------------------------------------------------
def _fetch_statements_sync(ticker: str, years: int) -> dict[str, Any]:
    import yfinance as yf

    tk = yf.Ticker(ticker)
    income_frame = tk.financials
    balance_frame = tk.balance_sheet
    cash_frame = tk.cashflow

    income_years = list(income_frame.columns)[:years] if income_frame is not None and not income_frame.empty else []
    balance_years = list(balance_frame.columns)[:years] if balance_frame is not None and not balance_frame.empty else []
    cash_years = list(cash_frame.columns)[:years] if cash_frame is not None and not cash_frame.empty else []

    if not income_years and not balance_years and not cash_years:
        raise TickerNoData(f"yfinance 未返回任何报表数据：{ticker}")

    currency = None
    try:
        currency = (tk.info or {}).get("currency")
    except Exception:
        try:
            currency = tk.fast_info.get("currency")
        except Exception:
            currency = None

    return {
        "income_statement": _extract(income_frame, _INCOME_ROWS, income_years),
        "balance_sheet": _extract(balance_frame, _BALANCE_ROWS, balance_years),
        "cash_flow": _extract(cash_frame, _CASHFLOW_ROWS, cash_years),
        "currency": currency,
    }


def apply_pe(metrics: dict[str, float | None], quote_pe: float | None) -> dict[str, float | None]:
    """PE 取口径优先级：行情源的 TTM PE（市场通行口径）→ 否则退回财年口径。"""
    metrics["pe_ratio"] = quote_pe if quote_pe is not None else metrics.get("pe_ratio_fiscal")
    return metrics


def fetch_financials(ticker: str, years: int = 3) -> FinancialMetrics:
    """yfinance 主路径：报表 + 报价 + 指标。失败抛 YahooUnavailable / TickerNoData。"""
    if is_cooling():
        raise YahooUnavailable("Yahoo 处于冷却期（此前连续失败）")

    started = time.perf_counter()
    try:
        statements = _with_timeout(_fetch_statements_sync, _FETCH_TIMEOUT, ticker, years)
    except (YahooUnavailable, TickerNoData):
        _register_failure("statements fetch failed")
        raise
    except Exception as exc:
        _register_failure(f"{type(exc).__name__}: {exc}")
        raise YahooUnavailable(f"yfinance 抓取异常：{type(exc).__name__}: {exc}") from exc

    elapsed = time.perf_counter() - started
    income = statements["income_statement"]
    balance = statements["balance_sheet"]
    cash_flow = statements["cash_flow"]
    fiscal_years = list(income.keys()) or list(balance.keys())

    quote = fetch_quote(ticker)
    market_cap = quote.get("market_cap")
    metrics = compute_ratios(income, balance, market_cap, fiscal_years)
    apply_pe(metrics, quote.get("pe_ratio"))

    _register_success()
    m.tool_calls_total.labels(tool="yahoo_finance", status="success").inc()
    m.tool_duration_seconds.labels(tool="yahoo_finance").observe(time.perf_counter() - started)
    logger.info(
        "yfinance ok | ticker={} years={} revenue={} 耗时={:.1f}s",
        ticker, fiscal_years[:1], (income.get(fiscal_years[0], {}) if fiscal_years else {}).get("revenue"), elapsed,
    )
    return FinancialMetrics(
        ticker=ticker.upper(),
        currency=statements.get("currency") or quote.get("currency"),
        fiscal_years=fiscal_years,
        income_statement=income,
        balance_sheet=balance,
        cash_flow=cash_flow,
        key_metrics=metrics,
        market_cap=market_cap,
        stock_price=quote.get("stock_price"),
        data_source="yfinance",
        quote_source=quote.get("quote_source"),
    )


# ---------------------------------------------------------------------------
# CrewAI 工具
# ---------------------------------------------------------------------------
class _YahooInput(BaseModel):
    ticker: str = Field(..., description="股票代码，如 AAPL")


class YahooFinanceTool(BaseTool):
    name: str = "yahoo_finance"
    description: str = (
        "获取指定 ticker 的近 3 年财务数据（营收/净利润/毛利/经营现金流/资产/负债/股东权益）"
        "以及当前股价、市值、PE、ROE、毛利率、营收增长率。返回 JSON。"
    )
    args_schema: type[BaseModel] = _YahooInput

    def _run(self, ticker: str) -> str:
        try:
            data = fetch_financials(ticker)
        except Exception as exc:
            m.tool_calls_total.labels(tool="yahoo_finance", status="failed").inc()
            return json.dumps(
                {"error": f"{type(exc).__name__}: {exc}", "ticker": ticker}, ensure_ascii=False
            )
        return data.model_dump_json()
