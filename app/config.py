"""全局配置：从 .env 读取，pydantic-settings 自动映射环境变量。

对应需求文档《Agent 驱动的自动化投研系统》：
- 3.2 技术栈：CrewAI + DeepSeek（V4 Flash 默认 / Pro 高质量生成）+ Celery + Redis
- 3.3/3.4 工作流与 Agent：每个 Agent 的 LLM 档位、工具集与输出校验
- 3.6 错误处理：tenacity 指数退避 + 任务超时保护（900s）
- 3.9 降级矩阵：SEC → Yahoo Finance；MinerU → PyPDF2/pdfplumber → Yahoo Finance
- 3.10 接口：按 token 限制每分钟新建任务数

说明：本项目为「文本 + 多 Agent 工作流」，无向量检索环节，
因此不再包含 Milvus / 本地 embedding / rerank 相关配置。
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_file 列表：后列文件优先，故 .env.local 置于其后以覆盖 .env 中的配置
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "investment-research-agent"
    app_version: str = "0.1.0"
    debug: bool = False

    # ---- LLM：DeepSeek（OpenAI 兼容接口，需求 3.2）----
    # 模型 ID 以 DeepSeek 开放平台当前可用值为准：V4 Flash = 默认档（快/性价比），
    # V4 Pro = 高质量生成档（复杂推理与报告撰写），均支持 function calling。
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"     # 默认档：信息搜集 / 财报分析 Agent
    deepseek_model_pro: str = "deepseek-v4-pro"   # 高质量档：报告撰写 Agent（需求 3.4）
    llm_temperature: float = 0.1
    llm_timeout: float = 90.0                 # 单次调用上限；Pro 撰写报告耗时更长
    llm_max_tokens: int = 4096                # 报告生成需要长输出，故高于问答类场景
    llm_max_retries: int = 1                  # SDK 层重试次数（业务层重试见 tool_max_retries）
    llm_circuit_threshold: int = 3            # 连续失败多少次后打开熔断（故障快速降级）
    llm_circuit_cooldown: float = 30.0        # 熔断冷却秒数，期间 LLM 调用毫秒级失败
    report_writer_llm_tier: str = "pro"       # 报告撰写档位（成本敏感可改 flash）
    report_max_tokens: int = 8192             # 报告正文较长，单独给更大的输出上限（含 reasoning 预算）
    report_llm_timeout: float = 240.0         # Pro 生成完整报告耗时较长，单独放宽超时

    # ---- Celery 后台任务队列（需求 3.1 架构 / 3.2 技术栈）----
    celery_broker_url: str = "redis://localhost:6380/1"      # broker 独立 db，避免与缓存混用
    celery_result_backend: str = "redis://localhost:6380/2"  # 结果后端（任务状态回查）
    celery_queue: str = "research"            # 队列名（worker 只消费该队列）
    celery_worker_concurrency: int = 3        # 需求 N7：最多 3 个任务同时运行
    celery_task_time_limit: int = 900         # 需求 3.6：硬超时 15 分钟，超时标记 failed
    celery_task_soft_time_limit: int = 840    # 软超时提前 60s，便于写日志与清理中间产物
    celery_task_max_retries: int = 3          # 需求 F6/N1：失败自动重试（任务成功率 >95%）
    celery_retry_backoff: int = 2             # 指数退避基数（秒）
    celery_retry_backoff_max: int = 60        # 退避上限（秒）
    celery_result_expires: int = 86400        # 结果保留 24h
    celery_task_acks_late: bool = True        # worker 崩溃时任务重回队列（提升成功率）
    celery_worker_prefetch_multiplier: int = 1  # 长任务场景：一次只预取一个任务，避免堆积

    # ---- 任务编排与超时保护（需求 3.6 / 3.7 / 3.10）----
    task_timeout_seconds: int = 900           # 单任务超时上限（与 Celery 硬超时保持一致）
    task_estimated_seconds: int = 480         # 创建任务时返回的预估耗时（8 分钟，需求 3.10）
    max_concurrent_tasks: int = 3             # 需求 N7：应用层并发闸门（与 worker 并发一致）
    step_max_retries: int = 2                 # 需求 3.7：Agent 输出校验失败最多重试 2 次
    continue_on_step_failure: bool = True     # 需求 3.7：仍失败则记录并跳过，后续 Agent 用已有数据继续

    # ---- SEC EDGAR（10-K 年报下载，需求 3.3 Task 2 / 3.9）----
    sec_user_agent: str = "investment-research-agent/0.1 (contact: your-email@example.com)"
    sec_edgar_base_url: str = "https://data.sec.gov"     # submissions / companyconcept 接口
    sec_archives_base_url: str = "https://www.sec.gov"   # Archives 全文索引与 filing 原始文件
    sec_api_key: str = ""                     # 可选：sec-api.io 商业网关（留空则直连 EDGAR）
    sec_api_base_url: str = "https://api.sec-api.io"
    sec_filing_type: str = "10-K"
    sec_request_interval: float = 0.15        # SEC 官方限速 10 req/s，本地留足余量
    sec_timeout: float = 30.0
    sec_max_retries: int = 3
    sec_filings_dir: str = "./data/filings"   # 年报 PDF/HTML 落盘目录

    # ---- MinerU：财报 PDF 结构化解析（需求 3.2 首选 / 3.9 降级）----
    mineru_enabled: bool = True
    mineru_backend: str = "pipeline"          # pipeline（CPU 可用）/ vlm-transformers / vlm-sglang-client
    mineru_command: str = "mineru"            # CLI 入口；旧版 magic-pdf 可在此覆盖
    mineru_api_url: str = ""                  # 走 HTTP 服务时填写；留空则调用本地 CLI
    mineru_output_dir: str = "./data/parsed"  # 结构化 JSON + 表格/图片产物目录
    mineru_lang: str = "en"
    mineru_timeout: float = 300.0             # 单份财报解析上限（复杂 PDF 较慢，超时即降级）
    pdf_fallback_enabled: bool = True         # 需求 3.9：降级 PyPDF2 + pdfplumber

    # ---- Yahoo Finance：备用数据源（需求 3.3 / 3.9）----
    yahoo_finance_enabled: bool = True
    yahoo_finance_timeout: float = 15.0
    yahoo_finance_max_retries: int = 2
    yahoo_finance_cache_ttl_seconds: int = 3600   # 股价/市值类短缓存

    # ---- 工具重试与降级（tenacity，需求 3.6 / 3.9）----
    tool_timeout: float = 60.0
    tool_max_retries: int = 3                 # stop_after_attempt(3)
    tool_retry_min_wait: float = 2.0          # wait_exponential(multiplier=1, min=2, max=10)
    tool_retry_max_wait: float = 10.0

    # ---- 缓存与产出（需求 N2：财报数据走缓存时 <5 分钟）----
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600                 # 通用缓存（搜索/新闻等易变数据）
    financials_cache_ttl_seconds: int = 86400     # 解析出的财务数据结构缓存 24h
    filings_cache_ttl_seconds: int = 604800       # 已下载年报文件缓存 7 天（10-K 不频繁变更）
    report_output_dir: str = "./data/reports"     # 最终报告落盘目录
    report_format: str = "markdown"               # markdown / pdf

    # ---- PostgreSQL（任务状态 / 中间产出 / 报告，需求 3.8）----
    pg_host: str = "localhost"
    # 默认端口 5433：项目一的 rag-postgres 占用 5432；容器内端口仍为 5432（见 docker-compose.infra.yml）
    pg_port: int = 5433
    pg_user: str = "research"
    pg_password: str = "researchpass"
    pg_database: str = "research"
    pg_pool_min: int = 2
    pg_pool_max: int = 20

    # ---- Redis（缓存 / 限流令牌桶；Celery 另用 db1、db2）----
    # 默认端口 6380：本机项目一（RAG-Agent）长期运行并占用 6379，故本项目改用 6380 以免互相干扰
    redis_url: str = "redis://localhost:6380/0"

    # ---- 认证（需求 N6：API 需鉴权，防止未授权触发任务消耗资源）----
    api_token: str = "dev-token-change-me"
    admin_token: str = "admin-token-change-me"   # 审计/管理接口（只读）专用
    auth_debug_log: bool = False           # 排查用：打印真实收到的 Authorization 头（明文，仅调试期开启）

    # ---- 限流（需求 N6 / 3.10）----
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 60           # 单身份每分钟请求数（状态/报告查询类接口）
    rate_limit_burst: int = 20                # 单身份突发容量
    task_create_per_minute: int = 1           # 需求 3.10：每个 token 每分钟最多触发 1 个新任务
    task_create_burst: int = 1                # 新建任务的突发容量（默认不允许突发）
    rate_limit_global_per_minute: int = 0     # 全局每分钟上限（0=关闭；保护昂贵的 LLM 实例）
    rate_limit_global_burst: int = 0          # 全局突发容量（0 时取 per_minute/2）

    # ---- 审计与人工干预（需求 F5）----
    audit_enabled: bool = True                # 每一步中间产出落库（company_profile / financial_metrics）
    human_intervention_enabled: bool = True   # 支持在步骤间人工介入修正后继续执行

    # ---- 可观测性（需求 N4：LangSmith 链路追踪 + 结构化日志）----
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "investment-research-agent"
    prometheus_url: str = "http://localhost:9090"

    @property
    def pg_dsn(self) -> str:
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    @property
    def llm_ready(self) -> bool:
        return bool(self.deepseek_api_key and not self.deepseek_api_key.startswith("sk-xxxx"))

    def llm_model(self, tier: str = "flash") -> str:
        """按档位取模型名：报告撰写用 pro，其余默认 flash（需求 3.2 / 3.4）。"""
        return self.deepseek_model_pro if str(tier).lower() == "pro" else self.deepseek_model


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
