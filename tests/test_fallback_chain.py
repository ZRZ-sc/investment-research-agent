"""数据源降级链：yfinance → SEC XBRL → 检索兜底 → degraded。

用 monkeypatch 切断网络，只验证编排逻辑：
- 主源失败必须切到备用源，并把降级原因记录到 notes（可审计）
- 全部失败必须 degraded=true 且 data_source=none（不是抛异常、也不是编数据）
- 缺 ticker 时快速降级，不调用任何外部数据源
"""
import pytest

import app.agents.financial_analyst as fa
from app.tools.yahoo_finance import FinancialMetrics, TickerNoData, YahooUnavailable


def _sec_metrics() -> FinancialMetrics:
    return FinancialMetrics(
        ticker="AAPL", currency="USD", fiscal_years=["2025"],
        income_statement={"2025": {"revenue": 416161000000.0, "net_income": 112010000000.0}},
        balance_sheet={"2025": {"stockholders_equity": 73733000000.0}},
        key_metrics={"pe_ratio": 40.52, "roe": 1.5191},
        market_cap=4.9e12, stock_price=336.7,
        data_source="sec_xbrl", quote_source="sina",
    )


def test_yfinance_failure_falls_back_to_sec(monkeypatch):
    """主源（yfinance）失败 → 自动切 SEC，结果不带 degraded，但原因记入 notes。"""
    def boom(*args, **kwargs):
        raise YahooUnavailable("Yahoo 处于冷却期（此前连续失败）")

    monkeypatch.setattr(fa, "fetch_financials", boom)
    monkeypatch.setattr(fa, "fetch_sec_financials", lambda *a, **k: _sec_metrics())
    monkeypatch.setattr(fa, "_interpret", lambda data: fa._Interpretation())

    result = fa.collect_financial_analysis("AAPL", "Apple Inc.")
    assert result.data_source == "sec_xbrl"
    assert result.quote_source == "sina"
    assert result.degraded is False
    assert result.key_metrics["pe_ratio"] == 40.52
    assert any("yfinance" in note for note in result.notes), "降级原因必须可审计"


def test_all_sources_fail_marks_degraded_without_raising(monkeypatch):
    """结构化源与检索兜底都失败 → degraded=true + data_source=none，且不抛异常。"""
    def raise_no_data(message):
        def _inner(*args, **kwargs):
            raise TickerNoData(message)
        return _inner

    monkeypatch.setattr(fa, "fetch_financials", raise_no_data("yfinance 未返回任何报表数据"))
    monkeypatch.setattr(fa, "fetch_sec_financials", raise_no_data("SEC 映射表中没有该 ticker"))
    monkeypatch.setattr(fa, "_extract_from_search", lambda *a, **k: None)  # 检索兜底也无结果

    result = fa.collect_financial_analysis("ZZZZ", "ZZZZ Corp")
    assert result.degraded is True
    assert result.data_source == "none"
    assert result.has_any_data() is False
    assert "yfinance" in (result.error or "")
    assert "SEC" in (result.error or "")


def test_missing_ticker_degrades_without_calling_sources(monkeypatch):
    """信息搜集没解析出 ticker 时，不应调用任何数据源，直接降级。"""
    monkeypatch.setattr(fa, "fetch_financials", lambda *a, **k: pytest.fail("缺少 ticker 时不应访问数据源"))
    monkeypatch.setattr(fa, "fetch_sec_financials", lambda *a, **k: pytest.fail("缺少 ticker 时不应访问数据源"))

    result = fa.collect_financial_analysis(None, "无代码公司")
    assert result.degraded is True
    assert "ticker" in (result.error or "")
