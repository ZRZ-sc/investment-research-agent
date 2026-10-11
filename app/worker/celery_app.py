"""Celery 实例（需求 3.1 后台任务队列 / 3.6 超时保护 / N7 并发控制）。

导出 `celery_app`，其点分路径 `app.worker.celery_app` 与环境变量 CELERY_APP 对齐
（Dockerfile 的 worker 角色、启动命令、部署脚本都引用该路径）。

启动 worker：
    .venv\\Scripts\\python.exe -m celery -A app.worker.celery_app worker -Q research -c 3 -l INFO
"""
from celery import Celery

from app.config import settings

celery_app = Celery(
    "research",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.worker.tasks"],  # 任务模块（worker 启动时导入，日志里可见 [tasks]）
)

celery_app.conf.update(
    # ---- 队列与并发（需求 N7：最多 3 个任务同时运行）----
    task_default_queue=settings.celery_queue,
    task_routes={"app.worker.tasks.*": {"queue": settings.celery_queue}},
    worker_concurrency=settings.celery_worker_concurrency,
    worker_prefetch_multiplier=settings.celery_worker_prefetch_multiplier,
    # ---- 超时保护（需求 3.6：硬超时 900s，软超时提前 60s 便于收尾）----
    task_time_limit=settings.celery_task_time_limit,
    task_soft_time_limit=settings.celery_task_soft_time_limit,
    # ---- 可靠性（需求 F6/N1：任务成功率 >95%）----
    task_acks_late=settings.celery_task_acks_late,
    task_reject_on_worker_lost=True,
    task_default_retry_delay=settings.celery_retry_backoff,
    broker_connection_retry_on_startup=True,
    # ---- 序列化与结果 ----
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=settings.celery_result_expires,
    task_track_started=True,  # 让状态查询能看到 STARTED（running）
    timezone="Asia/Shanghai",
    enable_utc=True,
)
