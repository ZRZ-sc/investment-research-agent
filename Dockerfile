# Agent 驱动的自动化投研系统 —— 应用镜像（API 与 Celery Worker 共用同一镜像）
#
# 构建（两个角色 target，依赖层共享构建缓存）：
#   docker build --target api    -t research-agent-api:latest .
#   docker build --target worker -t research-agent-worker:latest .
#   # 也可只构建一次，用 compose 覆盖 command 切换角色：
#   #   services: { api: {command: uvicorn ...}, worker: {command: celery ...} }
#   # 国内网络建议加 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#
# 说明：
#   - 不含本地模型：本项目无嵌入/重排模型（对比 RAG 项目），仅调用 DeepSeek / SEC / Yahoo 远程接口
#   - MinerU 依赖 opencv 等图像库，slim 基础镜像缺 libgl1 / libglib2.0-0，需显式安装
#   - 非 root 运行；tini 作 init 转发信号（Worker 收到 SIGTERM 才会优雅收尾，避免任务被硬杀）
FROM python:3.12-slim AS base

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TMPDIR=/tmp

# libgomp1: numpy 等运行时依赖；libgl1 + libglib2.0-0: MinerU(opencv) 运行时依赖；
# tini: 正确转发信号（Celery 优雅退出）
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app

# ---- 依赖层（先装依赖再拷代码，充分利用构建缓存） ----
COPY requirements.txt ./
RUN pip install -r requirements.txt --index-url "$PIP_INDEX_URL"

# ---- 代码层 ----
COPY app ./app
COPY scripts ./scripts

# 容器内默认配置：中间件按服务名寻址（对应 docker-compose.infra.yml）
ENV APP_NAME=investment-research-agent \
    PG_HOST=postgres \
    PG_PORT=5432 \
    REDIS_URL=redis://redis:6379/0 \
    CELERY_BROKER_URL=redis://redis:6379/1 \
    CELERY_RESULT_BACKEND=redis://redis:6379/2 \
    CELERY_QUEUE=research \
    CELERY_WORKER_CONCURRENCY=3 \
    CELERY_TASK_TIME_LIMIT=900 \
    SEC_USER_AGENT="investment-research-agent/0.1 (contact: your-email@example.com)" \
    SEC_FILINGS_DIR=/srv/data/filings \
    MINERU_ENABLED=true \
    MINERU_OUTPUT_DIR=/srv/data/parsed \
    REPORT_OUTPUT_DIR=/srv/data/reports \
    PROMETHEUS_URL=http://prometheus:9090

# 数据目录（年报 PDF、MinerU 结构化产物、最终报告）由卷挂载承接
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /srv/data/filings /srv/data/parsed /srv/data/reports \
 && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]

# =============================================================================
# 角色一：API（FastAPI，默认 target）
# =============================================================================
FROM base AS api

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "15"]

# =============================================================================
# 角色二：Celery Worker（后台任务队列，需求 3.1 / 3.6 / N7）
#   - 并发 3（N7）、硬超时 900s（3.6）、失败重试（F6）均由 app 侧的 Celery 配置统一声明，
#     此处只负责进程入口与队列订阅
#   - CELERY_APP 需与代码中的 Celery 实例路径一致（模块尚未落地时以 .env 覆盖即可）
# =============================================================================
FROM base AS worker

ENV CELERY_APP=app.worker.celery_app

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD celery -A "$CELERY_APP" inspect ping -d "celery@$HOSTNAME" > /dev/null 2>&1 || exit 1

# sh -c + exec：让 celery 成为 PID 1 的子进程直接接管信号（配合 tini 实现优雅退出）
CMD ["sh", "-c", "exec celery -A \"$CELERY_APP\" worker -Q \"$CELERY_QUEUE\" -c \"$CELERY_WORKER_CONCURRENCY\" -l INFO --max-tasks-per-child=50"]
