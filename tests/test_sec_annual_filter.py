"""SEC 年报口径过滤。

实测坑：公司 facts 里混有季度数据（Apple FY2018 Q3 的 532.65 亿被标成 FY），
若不过滤跨度，季度值会被当成年度营收；另外年报口径必须同时接受
10-K（美国本土）与 20-F（外国私人发行人，如阿里巴巴）。
"""
from app.tools.sec_financials import _annual_points

ANNUAL_10K = {"form": "10-K", "fp": "FY", "start": "2024-09-29", "end": "2025-09-27", "val": 416161000000, "fy": 2025}
QUARTER_IN_10K = {"form": "10-K", "fp": "FY", "start": "2025-04-01", "end": "2025-06-28", "val": 53265000000, "fy": 2025}
QUARTER_10Q = {"form": "10-Q", "fp": "Q3", "start": "2025-04-01", "end": "2025-06-28", "val": 53000000000, "fy": 2025}
ANNUAL_20F = {"form": "20-F", "fp": "FY", "start": "2024-04-01", "end": "2025-03-31", "val": 148401000000, "fy": 2025}


def test_only_annual_points_survive():
    """只保留整年数据点：10-K 年报 + 20-F 年报；排除季报与混入 10-K 的季度值。"""
    points = _annual_points([ANNUAL_10K, QUARTER_IN_10K, QUARTER_10Q, ANNUAL_20F], duration=True)
    assert set(points) == {"2025-09-27", "2025-03-31"}
    assert points["2025-09-27"][0] == 416161000000.0
    assert points["2025-03-31"][0] == 148401000000.0


def test_quarterly_only_data_yields_nothing():
    """只有季度数据时返回空字典（上层据此判定"无可用年报数据"）。"""
    assert _annual_points([QUARTER_10Q, QUARTER_IN_10K], duration=True) == {}


def test_balance_sheet_instant_points_have_no_span_requirement():
    """资产负债表是时点值（无 start），不应被跨度过滤误杀。"""
    instant = {"form": "10-K", "fp": "FY", "end": "2025-09-27", "val": 359241000000, "fy": 2025}
    assert _annual_points([instant], duration=False)["2025-09-27"][0] == 359241000000.0
    # 按流量科目口径解析同一批数据会被跨度规则过滤掉 —— 这正是保护生效
    assert _annual_points([instant], duration=True) == {}
