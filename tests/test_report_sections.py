"""报告 8 章完整性。

报告是最终交付物：LLM 生成 7 章 + 代码追加第 8 章附录（保证数字零失真）。
本测试保证两条路径（正常模板 / 降级模板）都产出完整 8 章，
因为 `_missing_sections()` 正是线上判定"要不要重试"的依据。
"""
from app.agents.report_writer import (
    _appendix,
    _header,
    _missing_sections,
    _template_body,
)

PROFILE = {
    "company_name": "Apple Inc.", "ticker": "AAPL", "sector": "Technology / Consumer Electronics",
    "description": "公司简介", "products": ["iPhone", "iPad"], "competitors": ["Samsung"],
    "recent_news": [{"date": "2025-09-09", "title": "iPhone 17 发布", "summary": "摘要", "url": "https://example.com"}],
    "data_sources": ["bing"], "degraded": False,
}
FINANCIALS = {
    "ticker": "AAPL", "data_source": "sec_xbrl", "quote_source": "sina",
    "fiscal_years": ["2025", "2024"],
    "income_statement": {
        "2025": {"revenue": 416161000000.0, "net_income": 112010000000.0,
                 "gross_profit": 195201000000.0, "operating_income": 133050000000.0, "eps": 7.46},
    },
    "balance_sheet": {"2025": {"total_assets": 359241000000.0, "total_liabilities": 285508000000.0,
                               "stockholders_equity": 73733000000.0}},
    "cash_flow": {"2025": {"operating_cash_flow": 111482000000.0, "capex": 12715000000.0,
                           "free_cash_flow": 98767000000.0}},
    "key_metrics": {"pe_ratio": 40.52, "roe": 1.5191, "gross_margin": 0.4691},
    "market_cap": 4.9e12, "stock_price": 336.7, "degraded": False,
}


def test_template_body_contains_all_seven_llm_sections():
    """LLM 不可用时的结构化模板也必须含 7 个二级标题（否则会被判为不完整）。"""
    body = _template_body("Apple Inc.", "AAPL", PROFILE, FINANCIALS, "LLM 超时")
    assert _missing_sections(body) == []


def test_full_report_has_eight_sections_and_real_appendix_numbers():
    """头 + 正文 + 附录拼出的完整报告：8 章齐全，且附录数字来自原始 JSON。"""
    content = "\n".join([
        _header("Apple Inc.", "AAPL", [], True),
        _template_body("Apple Inc.", "AAPL", PROFILE, FINANCIALS, None),
        _appendix(PROFILE, FINANCIALS, "deepseek-v4-pro"),
    ])
    assert _missing_sections(content) == []
    assert "## 8. 附录：原始数据" in content
    for table in ("8.1 利润表", "8.2 资产负债表", "8.3 现金流量表", "8.4 核心指标", "8.5 数据来源与限制"):
        assert table in content
    # 亿美元口径、与原始 JSON 一致（4,161.61 / 1,120.10）
    assert "4,161.61" in content
    assert "1,120.10" in content
    assert "sec_xbrl" in content


def test_degraded_upstream_adds_data_limited_warning():
    """上游降级 → 报告顶部必须出现「数据来源受限」清单（F5/降级可审计）。"""
    header = _header("ZZZZ Corp", "ZZZZ", ["财务数据：所有数据源均不可用（yfinance/SEC/检索）"], True)
    assert "数据来源受限" in header
    assert "财务数据" in header
    # 无降级项时不应出现该警示
    assert "数据来源受限" not in _header("Apple Inc.", "AAPL", [], True)
