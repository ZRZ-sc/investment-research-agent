"""报价占位数据校验。

评测暴露的真实问题：新浪对**不存在的 ticker** 会返回一行占位数据
`ZZZZ,1.0000,0.00,...,0,0,0,0.00,--`（价格 1.0、市值 0），
若不校验就会被当成真实行情，导致"无数据公司"被判成有数据。
"""
import pytest

from app.tools.yahoo_finance import _sane_quote


def test_sina_placeholder_row_is_rejected_for_quote_sources():
    """新浪/腾讯必须给出有效市值才算真行情（占位行只有价格 1.0、市值 0）。"""
    placeholder = {"stock_price": 1.0, "market_cap": 0.0, "pe_ratio": 0.0, "quote_source": "sina"}
    with pytest.raises(ValueError):
        _sane_quote(dict(placeholder), require_market_cap=True)


def test_zero_price_and_market_cap_are_rejected():
    """价格与市值都无效 → 直接判为无效报价。"""
    with pytest.raises(ValueError):
        _sane_quote({"stock_price": 0.0, "market_cap": 0.0})
    with pytest.raises(ValueError):
        _sane_quote({"stock_price": None, "market_cap": None})


def test_valid_quote_passes_and_invalid_fields_are_cleared():
    """正常行情通过；非正的字段被清空为 None（宁可为空也不给错数字）。"""
    quote = _sane_quote(
        {"stock_price": 336.7, "market_cap": 4.9e12, "pe_ratio": 40.5}, require_market_cap=True
    )
    assert quote["stock_price"] == 336.7
    assert quote["market_cap"] == 4.9e12

    cleaned = _sane_quote(
        {"stock_price": -1.0, "market_cap": 4.9e12, "pe_ratio": 0.0}, require_market_cap=True
    )
    assert cleaned["stock_price"] is None
    assert cleaned["pe_ratio"] is None
    assert cleaned["market_cap"] == 4.9e12
