"""Prometheus 指标定义：面向多 Agent 投研任务（需求文档 N1/N2/N3/N4）。

指标围绕三类核心观测对象组织：
  1) 任务维度：成功率（N1 >95%）、端到端耗时（N2 <8 分钟）、并发与排队（N7 并发上限 3）
  2) Agent 维度：单个 Agent 的执行时长与成功率（3.4 三个 Agent：信息搜集/财务分析/报告撰写）
  3) 工具维度：外部工具耗时、重试与降级（N3 工具可用性 / 3.9 降级矩阵）

命名与标签约定：
  - 单位一律为秒（Prometheus 最佳实践），直方图名以 `_seconds` 结尾
  - **不使用** task_id / company_name 作为标签，避免高基数拖垮 Prometheus；
    单任务明细请查 PostgreSQL（research_tasks / task_outputs）或 LangSmith 链路
"""
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response

# ---- 直方图分桶（按各环节真实量级设定，避免全量落进 +Inf 桶） ----
_TOOL_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0)
_AGENT_BUCKETS = (1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 180.0, 300.0, 480.0, 600.0, 900.0)
_TASK_BUCKETS = (30.0, 60.0, 120.0, 180.0, 240.0, 300.0, 360.0, 480.0, 600.0, 900.0)
_LLM_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 90.0, 120.0)
_API_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)
_PARSE_BUCKETS = (1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 180.0, 300.0, 600.0)

# ===========================================================================
# 任务（research task）
# ===========================================================================
research_tasks_total = Counter(
    "research_tasks_total",
    "投研任务总数（按终态计数）",
    ["status"],  # queued/running/completed/failed/need_human（超时按需求 3.6 记 failed）
)
research_task_duration_seconds = Histogram(
    "research_task_duration_seconds",
    "任务端到端执行时长（需求 N2：<8 分钟；财报走缓存 <5 分钟）",
    buckets=_TASK_BUCKETS,
)
research_task_timeouts_total = Counter(
    "research_task_timeouts_total",
    "任务超时次数（需求 3.6：超过 900s 标记 failed 并记录超时日志）",
)
research_task_success_rate = Gauge(
    "research_task_success_rate",
    "任务成功率（滑动窗口，含自动重试；需求 N1：>0.95）",
)
research_tasks_in_progress = Gauge(
    "research_tasks_in_progress",
    "当前运行中的任务数（需求 N7：并发上限）",
)
research_task_queue_length = Gauge(
    "research_task_queue_length",
    "等待执行的排队任务数（Celery 队列积压）",
)
research_steps_total = Counter(
    "research_steps_total",
    "工作流步骤执行次数（按状态）",
    ["step", "status"],  # step=profile/financials/report
)
research_step_duration_seconds = Histogram(
    "research_step_duration_seconds",
    "工作流单步（Task 1/2/3）执行时长",
    ["step"],
    buckets=_AGENT_BUCKETS,
)
research_step_retries_total = Counter(
    "research_step_retries_total",
    "工作流步骤重试次数（需求 F6：失败自动重试）",
    ["step"],
)

# ===========================================================================
# Agent（需求 3.4：信息搜集 / 财务分析 / 报告撰写）
# ===========================================================================
agent_execution_duration_seconds = Histogram(
    "agent_execution_duration_seconds",
    "Agent 执行时长（含其内部工具调用与 LLM 生成）",
    ["agent"],
    buckets=_AGENT_BUCKETS,
)
agent_executions_total = Counter(
    "agent_executions_total",
    "Agent 执行次数（按结果）",
    ["agent", "status"],  # success/failed/degraded
)
agent_validation_failures_total = Counter(
    "agent_validation_failures_total",
    "Agent 输出 Pydantic 校验失败次数（需求 3.7，失败会触发重跑）",
    ["agent"],
)

# ===========================================================================
# 工具（需求 N3：集成 ≥5 个工具，每个工具都有降级方案）
# ===========================================================================
tool_duration_seconds = Histogram(
    "tool_duration_seconds",
    "工具调用耗时（外部 API / 解析 / 计算）",
    ["tool"],
    buckets=_TOOL_BUCKETS,
)
tool_calls_total = Counter(
    "tool_calls_total",
    "工具调用次数（按结果）",
    ["tool", "status"],  # success/failed/timeout/fallback
)
tool_retries_total = Counter(
    "tool_retries_total",
    "工具重试次数（tenacity 指数退避，需求 3.6）",
    ["tool"],
)
tool_fallbacks_total = Counter(
    "tool_fallbacks_total",
    "工具降级次数（需求 3.9 降级矩阵）",
    ["tool", "fallback_tool"],
)

# ===========================================================================
# LLM 调用（DeepSeek Flash / Pro）
# ===========================================================================
llm_request_duration_seconds = Histogram(
    "llm_request_duration_seconds",
    "LLM 单次调用耗时",
    ["model", "agent"],
    buckets=_LLM_BUCKETS,
)
llm_tokens_total = Counter(
    "llm_tokens_total",
    "Token 消耗总量",
    ["model", "type"],  # type=prompt/completion
)
llm_failure_total = Counter(
    "llm_failure_total",
    "LLM 调用失败次数（含熔断短路，3.9：重试 3 次后返回模板占位）",
    ["stage"],
)

# ===========================================================================
# 数据源与解析环节（SEC / MinerU / Yahoo Finance）
# ===========================================================================
sec_requests_total = Counter(
    "sec_requests_total",
    "SEC EDGAR 请求次数（按端点与结果）",
    ["endpoint", "status"],  # endpoint=submissions/companyconcept/archives
)
sec_filings_downloaded_total = Counter(
    "sec_filings_downloaded_total",
    "成功下载的年报份数",
    ["filing_type"],  # 10-K
)
mineru_parse_duration_seconds = Histogram(
    "mineru_parse_duration_seconds",
    "财报 PDF 解析耗时（MinerU 首选 / PyPDF2 降级共用）",
    ["backend"],
    buckets=_PARSE_BUCKETS,
)
mineru_parse_total = Counter(
    "mineru_parse_total",
    "PDF 解析次数（按解析器与结果）",
    ["backend", "status"],  # backend=mineru/pypdf2/pdfplumber
)
yahoo_finance_requests_total = Counter(
    "yahoo_finance_requests_total",
    "Yahoo Finance 请求次数（备用数据源，需求 3.9）",
    ["endpoint", "status"],
)
reports_generated_total = Counter(
    "reports_generated_total",
    "生成的投资报告份数",
    ["format"],  # markdown/pdf
)

# ===========================================================================
# 缓存（需求 N2：财报数据走缓存）
# ===========================================================================
cache_hits_total = Counter(
    "cache_hits_total",
    "缓存命中次数",
    ["name"],  # filings/financials/search/report
)
cache_misses_total = Counter(
    "cache_misses_total",
    "缓存未命中次数",
    ["name"],
)

# ===========================================================================
# HTTP 接口（3.10：任务创建 / 状态查询 / 报告获取）
# ===========================================================================
api_requests_total = Counter(
    "api_requests_total",
    "API 请求总数",
    ["endpoint", "method", "status"],
)
api_request_duration_seconds = Histogram(
    "api_request_duration_seconds",
    "API 请求处理耗时",
    ["endpoint"],
    buckets=_API_BUCKETS,
)

# ---- 限流（429；app/services/rate_limit.py 依赖此指标名，勿改名）----
rate_limited_total = Counter(
    "rate_limited_total",
    "触发限流的请求数（需求 N6/N7）",
    ["scope"],
)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
