"""ticker 归一化与本地映射表。

覆盖评测中出过问题的名称解析路径：大小写、多余空格、公司后缀、中文别名，
以及"未命中必须返回 None（交给 LLM 兜底 + SEC 备案交叉验证）"这一关键契约。
"""
from app.tools.ticker_lookup import lookup_local, normalize


def test_normalize_handles_case_space_and_suffix():
    """归一化：小写、去标点、去公司后缀、压缩空格。"""
    assert normalize("Apple Inc.") == "apple"
    assert normalize("APPLE") == "apple"
    assert normalize("  apple  inc.  ") == "apple"
    assert normalize("腾讯") == "腾讯"          # 中文原样保留
    assert normalize("Tencent Holdings Limited") == "tencent"


def test_local_table_hits_english_and_chinese_aliases():
    """本地表命中：英文全名 / 简称 / 中文别名（中文名公司类别依赖这条）。"""
    for name, expected in (
        ("Apple Inc.", "AAPL"),
        ("apple", "AAPL"),
        ("苹果", "AAPL"),
        ("微软", "MSFT"),
        ("腾讯", "TCEHY"),
        ("Tencent Holdings Limited", "TCEHY"),
        ("台积电", "TSM"),
        ("特斯拉", "TSLA"),
    ):
        hit = lookup_local(name)
        assert hit is not None, f"{name} 应命中本地表"
        assert hit.ticker == expected, f"{name} -> {hit.ticker}，期望 {expected}"
        assert hit.source == "local"


def test_local_table_miss_returns_none():
    """未命中必须返回 None（由上层走 LLM 兜底 + SEC 备案验证），不能猜一个代码。"""
    for name in ("O'Reilly Automotive, Inc.", "3M Company", "AT&T Inc.", "Foobar Inc.", "ZZZZ Corp"):
        assert lookup_local(name) is None, f"{name} 不应命中本地表"
