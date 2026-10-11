"""FastAPI 入口：投研任务接口 + 探针 + Prometheus 指标。

| 接口 | 说明 |
|------|------|
| POST /api/v1/research | 创建投研任务（Bearer 鉴权 + 每 token 每分钟限 1 个，需求 3.10 / N6）|
| GET  /api/v1/research/{task_id} | 任务状态、进度与中间产出 |
| GET  /api/v1/research/{task_id}/report | 最终投资分析报告 |
| GET  /health | 存活探针（进程在不在）|
| GET  /ready | 就绪探针（PostgreSQL / Redis / Celery broker）|
| GET  /metrics | Prometheus 指标（不在 OpenAPI 中展示）|
| GET  / | **F7 极简 Web 界面**（app/static/index.html：提交任务 → 进度 → 报告）|
| GET  /static/* | 静态资源（含本地化的 marked.js，见 app/static/vendor/）|

启动：
    .venv\\Scripts\\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import research
from app.config import settings
from app.core import metrics as m
from app.core.errors import ApiError, ok
from app.core.logging import logger, request_id_var, setup_logging
from app.core.metrics import metrics_response
from app.db.postgres import close_pool, init_pool
from app.db.postgres import ping as postgres_ping
from app.db.redis import close_redis, init_redis, ping_broker
from app.db.redis import ping as redis_ping


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(
        json_logs=os.getenv("LOG_JSON", "true").lower() != "false",
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    logger.info("starting {} v{}", settings.app_name, settings.app_version)

    # 依赖不可用时**不阻塞启动**：/ready 会如实返回 not_ready，交由编排层做就绪门禁
    try:
        await init_pool()
    except Exception as exc:
        logger.error("postgres init failed: {}", exc)
    try:
        await init_redis()
    except Exception as exc:
        logger.error("redis init failed: {}", exc)

    yield

    await close_redis()
    await close_pool()
    logger.info("shutdown complete")


app = FastAPI(
    title="Agent 驱动的自动化投研系统",
    description="输入公司名称，自动完成信息搜集 → 财报分析 → 报告撰写",
    version=settings.app_version,
    lifespan=lifespan,
)

app.include_router(research.router)

# ---------------------------------------------------------------------------
# 静态资源与 Web 界面（需求 F7：极简前端，演示/面试可直接用）
# ---------------------------------------------------------------------------
STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# 中间件：request_id 贯穿 + 结构化访问日志 + HTTP 指标
# ---------------------------------------------------------------------------
@app.middleware("http")
async def access_log_and_metrics(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - started

    route = request.scope.get("route")
    endpoint = getattr(route, "path", None) or request.url.path

    # /metrics 自身不计数，避免自观测噪声
    if endpoint != "/metrics":
        m.api_requests_total.labels(
            endpoint=endpoint, method=request.method, status=str(response.status_code)
        ).inc()
        m.api_request_duration_seconds.labels(endpoint=endpoint).observe(elapsed)

    response.headers["X-Request-ID"] = request_id
    logger.info(
        "http_access | {} {} -> {} in {:.1f}ms",
        request.method, request.url.path, response.status_code, elapsed * 1000,
    )
    request_id_var.reset(token)
    return response


# ---------------------------------------------------------------------------
# 统一错误响应（与 app/core/errors.py 的错误码体系一致）
# ---------------------------------------------------------------------------
@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    logger.warning("api error | {} {} | code={} msg={}", request.method, request.url.path, exc.code, exc.message)
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict(), headers=exc.headers or None)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "code": 422,
            "message": "请求参数校验失败",
            "data": {"errors": jsonable_encoder(exc.errors())[:5]},
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error | {} {} | {}", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500, content={"code": 500, "message": "服务器内部错误", "data": None}
    )


# ---------------------------------------------------------------------------
# 探针与指标
# ---------------------------------------------------------------------------
@app.get("/health", tags=["ops"], summary="存活探针")
async def health() -> dict:
    return ok({"status": "healthy", "app": settings.app_name, "version": settings.app_version})


@app.get("/ready", tags=["ops"], summary="就绪探针（依赖连通性）")
async def ready() -> JSONResponse:
    checks = {
        "postgres": await postgres_ping(),
        "redis": await redis_ping(),
        "celery_broker": await ping_broker(),
    }
    all_ready = all(checks.values())
    body = ok({"status": "ready" if all_ready else "not_ready", "checks": checks})
    if not all_ready:
        logger.warning("readiness degraded | {}", checks)
    return JSONResponse(status_code=200 if all_ready else 503, content=body)


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return metrics_response()


@app.get("/", include_in_schema=False)
async def index():
    """F7 Web 界面：输入公司名 → 轮询进度 → 渲染 Markdown 报告。"""
    index_file = STATIC_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    # 静态页缺失时退化为服务信息，便于排障
    return ok(
        {
            "service": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "create_task": "POST /api/v1/research",
            "hint": "app/static/index.html 不存在，Web 界面未启用",
        }
    )
