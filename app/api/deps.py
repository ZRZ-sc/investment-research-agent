"""API 依赖：Bearer Token 认证。

## 鉴权格式（两种客户端写法不同，注意区分）

| 客户端 | 传参方式 |
|--------|---------|
| curl / 代码 | 请求头 `Authorization: Bearer dev-token-change-me`（**必须带 `Bearer ` 前缀**） |
| Swagger UI (`/docs`) | 点右上角 **Authorize**，在 `HTTPBearer` 弹窗里**只填 token**：`dev-token-change-me`，Swagger 会自动拼上 `Bearer ` |

使用标准 HTTPBearer 安全方案的意义：Swagger UI 才会渲染出可用的 Authorize 按钮
（普通 `Header()` 参数只会生成一个原样发送的输入框，容易漏掉 `Bearer ` 前缀导致 401）。
"""
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.core.errors import UnauthorizedError
from app.core.logging import logger
from app.core.masking import mask_token

# auto_error=False：不抛 FastAPI 默认的 403/401 结构，由本模块统一抛业务错误码
_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="HTTPBearer",
    description="填写 API Token（无需 Bearer 前缀，Swagger 会自动补全）",
)


def _mask(token: str | None) -> str:
    """脱敏展示：保留前 4 位与后 2 位，其余打码。"""
    if not token:
        return "-"
    if len(token) <= 8:
        return token[0] + "*" * (len(token) - 1)
    return f"{token[:4]}…{token[-2:]}(len={len(token)})"


async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """校验 Authorization: Bearer <token>。"""
    raw_header = request.headers.get("authorization")
    # HTTPBearer 解析成功时 credentials.scheme == "Bearer"，credentials.credentials 为 token
    token = credentials.credentials.strip() if credentials is not None else None
    matched = bool(token) and token == settings.api_token

    # ---- 排查用日志（在比对前打印真实收到的 Header，token 已脱敏） ----
    if settings.auth_debug_log:
        # 即使调试期也只记录脱敏值（需求文档要求禁止记录明文 token）
        logger.info("auth debug | raw Authorization header = {!r}", mask_token(raw_header))
    logger.info(
        "auth check | header_present={} scheme_ok={} token={} expected={} matched={}",
        raw_header is not None,
        credentials is not None,
        _mask(token),
        _mask(settings.api_token),
        matched,
    )
    if raw_header is not None and credentials is None:
        logger.warning(
            "auth failed: Authorization 头存在但格式不合法（缺少 'Bearer ' 前缀？）raw={!r}",
            mask_token(raw_header),
        )

    if not matched:
        raise UnauthorizedError(
            "未认证或token无效；请在请求头中传递 Authorization: Bearer <API_TOKEN>"
        )


async def verify_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """管理后台（F7）鉴权：使用独立的 ADMIN_TOKEN，与业务 API_TOKEN 分离。"""
    raw_header = request.headers.get("authorization")
    token = credentials.credentials.strip() if credentials is not None else None
    matched = bool(token) and token == settings.admin_token
    logger.info(
        "admin auth check | header_present={} token={} matched={}",
        raw_header is not None, _mask(token), matched,
    )
    if not matched:
        raise UnauthorizedError(
            "管理后台鉴权失败；请在请求头中传递 Authorization: Bearer <ADMIN_TOKEN>"
        )
