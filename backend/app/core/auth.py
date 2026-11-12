"""认证上下文与 Cookie / CSRF 辅助。

会话设计（见 plan.txt §3「登录与接口约定」）：

* 同源 Cookie 会话：Cookie 只保存随机会话 ID，会话数据存 Redis。
* ``HttpOnly`` + ``Secure`` + ``SameSite=Lax``；CSRF Token 放在可读 Cookie，
  前端在写请求头 ``X-CSRF-Token`` 回传，服务端校验「请求头 = Cookie = 会话内记录」。
* 非安全方法额外校验 ``Origin`` / ``Referer``。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request, Response

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.redis import anonymous_csrf_exists, read_session
from app.core.security import constant_time_equals

UNSAFE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """中心库中的登录用户。"""

    user_id: int
    email: str
    display_name: str
    status: str
    is_platform_admin: bool = False
    locale: str = "zh-CN"


@dataclass(frozen=True, slots=True)
class TenantAccessInfo:
    """用户对某租户的访问信息（中心库侧）。"""

    tenant_id: int
    tenant_code: str
    tenant_name: str
    tenant_status: str
    timezone: str
    member_id: int
    member_status: str
    role_summary: tuple[str, ...] = ()
    member_no: int | None = None


def allowed_origins() -> set[str]:
    origins = set(settings.cors_origins)
    if settings.app_origin:
        origins.add(settings.app_origin)
    return origins


def verify_request_origin(request: Request) -> None:
    """写请求的来源校验；不含 Origin 的客户端（如脚本）按 Referer 判断。"""
    if request.method not in UNSAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin:
        if origin not in allowed_origins():
            raise AppError(ErrorCode.AUTH_ORIGIN_INVALID)
        return
    referer = request.headers.get("referer")
    if not referer:
        # 非浏览器客户端（CLI / Worker 内部调用）没有 Origin，交由 CSRF 与会话校验
        return
    if not any(referer.startswith(item) for item in allowed_origins()):
        raise AppError(ErrorCode.AUTH_ORIGIN_INVALID)


def is_csrf_exempt(path: str) -> bool:
    """回调 / 健康检查使用签名或公开校验，不走用户 CSRF。"""
    if path in {"/healthz", "/livez"}:
        return True
    # Agent 使用节点 Bearer 令牌认证，不携带浏览器 Cookie；不能要求 CSRF。
    if path.startswith(f"{settings.api_prefix}/agent/"):
        return True
    return path.startswith(f"{settings.api_prefix}/integrations/")


def verify_csrf(request: Request, session_payload: dict | None) -> None:
    header_token = request.headers.get("x-csrf-token", "")
    cookie_token = request.cookies.get(settings.csrf_cookie_name, "")
    if not header_token or not cookie_token or not constant_time_equals(header_token, cookie_token):
        raise AppError(ErrorCode.AUTH_CSRF_INVALID)
    if session_payload:
        expected = str(session_payload.get("csrf") or "")
        if not expected or not constant_time_equals(header_token, expected):
            raise AppError(ErrorCode.AUTH_CSRF_INVALID)
        return
    # 登录前：令牌必须由服务端签发且未过期
    if not anonymous_csrf_exists(header_token):
        raise AppError(ErrorCode.AUTH_CSRF_INVALID)


def load_session_payload(request: Request) -> dict | None:
    session_id = request.cookies.get(settings.session_cookie_name, "")
    if not session_id:
        return None
    return read_session(session_id)


def session_scope(payload: dict | None) -> str | None:
    """读取会话登录端：``tenant`` / ``platform``。

    兼容升级前创建的旧会话：旧会话没有 ``scope`` 字段，返回 ``None``，
    由调用方按“同时允许”处理；用户重新登录后即带显式 scope。
    """
    if not payload:
        return None
    value = payload.get("scope")
    return str(value) if value else None


def set_session_cookies(response: Response, session_id: str, csrf_token: str) -> None:
    max_age = settings.session_ttl_seconds
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_id,
        max_age=max_age,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_same_site,
        path="/",
    )
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_same_site,
        path="/",
    )


def set_csrf_cookie(response: Response, csrf_token: str) -> None:
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf_token,
        max_age=settings.session_ttl_seconds,
        httponly=False,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_same_site,
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


def ensure_bearer_disabled() -> None:
    """显式说明：浏览器接口不使用 Authorization 头，避免与 Cookie 会话混用。"""
    return None
