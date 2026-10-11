"""Agent 工具集（需求 3.3 / 3.4 / 3.5）。

- web_search：DuckDuckGo（免 Key）+ Wikipedia（免 Key），带 Bing 降级链（需求 3.9）
- ticker_lookup：本地映射表（28 家常见公司）+ LLM 兜底判断
- yahoo_finance：yfinance 三大报表 + 报价 + 指标计算（报价带新浪/腾讯降级）
- sec_financials：SEC EDGAR XBRL 结构化财务数据（仅 JSON，不下载 10-K）
"""
from app.tools.sec_financials import SecFinancialsTool, fetch_sec_financials
from app.tools.ticker_lookup import TickerLookupTool, lookup_ticker
from app.tools.web_search import (
    BingSearchTool,
    DuckDuckGoSearchTool,
    WikipediaSearchTool,
    gather_company_context,
    search_web,
    wikipedia_summary,
)
from app.tools.yahoo_finance import (
    FinancialMetrics,
    YahooFinanceTool,
    compute_ratios,
    fetch_financials,
    fetch_quote,
)

__all__ = [
    "BingSearchTool",
    "DuckDuckGoSearchTool",
    "FinancialMetrics",
    "SecFinancialsTool",
    "TickerLookupTool",
    "WikipediaSearchTool",
    "YahooFinanceTool",
    "compute_ratios",
    "fetch_financials",
    "fetch_quote",
    "fetch_sec_financials",
    "gather_company_context",
    "lookup_ticker",
    "search_web",
    "wikipedia_summary",
]
