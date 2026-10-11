"""统一错误码与异常体系（与需求文档 3.5 节一致）。

| 错误码 | 含义                          |
|-------|-------------------------------|
| 0     | 成功                          |
| 401   | 未认证或 token 无效           |
| 403   | 无权限（如查询他人订单）      |
| 429   | 请求过于频繁，触发限流        |
| 500   | 服务器内部错误                |
| 503   | 服务降级，暂时不可用          |
"""


class ApiError(Exception):
    """业务异常基类：携带对外错误码、HTTP 状态与可选响应头。"""

    def __init__(
        self,
        code: int,
        message: str,
        http_status: int = 400,
        data: dict | None = None,
        headers: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.data = data
        self.headers = headers or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "data": self.data}


class UnauthorizedError(ApiError):
    def __init__(self, message: str = "未认证或token无效", data=None):
        super().__init__(401, message, http_status=401, data=data)


class ForbiddenError(ApiError):
    def __init__(self, message: str = "无权限访问该资源", data=None):
        super().__init__(403, message, http_status=403, data=data)


class RateLimitError(ApiError):
    """429：触发限流（携带 Retry-After 响应头）。"""

    def __init__(
        self,
        message: str = "请求过于频繁，请稍后再试",
        data=None,
        headers: dict | None = None,
    ):
        super().__init__(429, message, http_status=429, data=data, headers=headers)


class NotFoundError(ApiError):
    def __init__(self, message: str = "未找到相关记录", data=None):
        super().__init__(404, message, http_status=404, data=data)


class InternalError(ApiError):
    def __init__(self, message: str = "服务器内部错误", data=None):
        super().__init__(500, message, http_status=500, data=data)


class DegradedError(ApiError):
    """服务降级：核心依赖不可用，返回兜底话术。"""

    def __init__(self, message: str = "抱歉，系统繁忙，请稍后再试", data=None):
        super().__init__(503, message, http_status=503, data=data)


def ok(data=None, message: str = "success") -> dict:
    """成功响应统一封装（需求文档 3.10：{"code": 0, "message": ..., "data": ...}）。"""
    return {"code": 0, "message": message, "data": data}
