"""结构化日志：Loguru + JSON 序列化，contextvars 注入 request_id 等全链路字段。"""
import sys
from contextvars import ContextVar

from loguru import logger

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")
session_id_var: ContextVar[str] = ContextVar("session_id", default="-")


def _ensure_utf8_stdout() -> None:
    """Windows 控制台默认 GBK：JSON 日志含 emoji/中文会抛 UnicodeEncodeError 并被静默丢弃。
    统一把 stdout/stderr 切到 UTF-8（errors=replace），保证日志可见。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _context_filter(record) -> bool:
    record["extra"]["request_id"] = request_id_var.get()
    record["extra"]["trace_id"] = trace_id_var.get()
    record["extra"]["user_id"] = user_id_var.get()
    record["extra"]["session_id"] = session_id_var.get()
    return True


def setup_logging(json_logs: bool = True, level: str = "INFO") -> None:
    logger.remove()
    _ensure_utf8_stdout()
    # 注意：不要设置 enqueue=True —— 它内部依赖 multiprocessing 命名管道，
    # 在受限沙箱（Windows sandbox / 容器）中会因无法创建管道抛 PermissionError，
    # 导致应用启动直接失败。单进程 uvicorn 无需异步日志队列。
    if json_logs:
        # loguru 内置 JSON 序列化（serialize=True），extra 字段自动包含 request_id 等
        logger.add(
            sys.stdout,
            filter=_context_filter,
            serialize=True,
            level=level,
        )
    else:
        logger.add(
            sys.stdout,
            filter=_context_filter,
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | "
                "req={extra[request_id]} | user={extra[user_id]} | {message}"
            ),
            level=level,
        )
    logger.debug("logging initialized")


def get_logger():
    return logger
