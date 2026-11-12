"""请求级上下文：请求标识、当前用户、租户上下文。

约束（见 plan.txt §2「租户上下文与连接管理」）：

* 租户上下文只在当前请求 / 当前 Worker 任务的 ContextVar 中保存，
  不存在可被其他请求覆盖的模块级全局变量。
* 未建立租户上下文时，业务查询必须直接失败（由 ``require_tenant_context`` 抛错）。
* ``TenantContext`` 不可变，创建后不允许修改。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.errors import AppError, ErrorCode


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


_request_id_var: ContextVar[str] = ContextVar("request_id", default="")
_current_user_id_var: ContextVar[int | None] = ContextVar("current_user_id", default=None)
_tenant_context_var: ContextVar["TenantContext | None"] = ContextVar("tenant_context", default=None)


def get_request_id() -> str:
    request_id = _request_id_var.get()
    if not request_id:
        request_id = new_request_id()
        _request_id_var.set(request_id)
    return request_id


def set_request_id(request_id: str) -> Token[str]:
    return _request_id_var.set(request_id)


def reset_request_id(token: Token[str]) -> None:
    _request_id_var.reset(token)


def get_current_user_id() -> int | None:
    return _current_user_id_var.get()


def set_current_user_id(user_id: int | None) -> Token[int | None]:
    return _current_user_id_var.set(user_id)


def reset_current_user_id(token: Token[int | None]) -> None:
    _current_user_id_var.reset(token)


@dataclass(frozen=True, slots=True)
class TenantDbRoute:
    """租户库连接路由。只从中心库受控映射读取，不信任请求参数。"""

    tenant_id: int
    host: str
    port: int
    database: str
    username: str
    password: str
    credentials_version: int = 1

    def cache_key(self) -> tuple[int, str, int, str, int]:
        return (
            self.tenant_id,
            self.host,
            self.port,
            self.database,
            self.credentials_version,
        )

    def sqlalchemy_url(self) -> str:
        from urllib.parse import quote_plus

        return (
            f"mysql+pymysql://{quote_plus(self.username)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.database}?charset=utf8mb4"
        )


@dataclass(frozen=True, slots=True)
class TenantContext:
    """一次请求 / 任务内不可变的租户上下文。"""

    tenant_id: int
    tenant_code: str
    tenant_name: str
    tenant_status: str
    timezone: str
    member_id: int
    member_status: str
    user_id: int
    db_route: TenantDbRoute
    permission_version: int
    permissions: frozenset[str] = frozenset()
    scope_grants: tuple[Any, ...] = ()  # tuple[ScopeGrant, ...]，见 core.access
    request_id: str = ""
    is_automation: bool = False
    actor_id: int | None = None
    feature_flags: frozenset[str] = frozenset()
    created_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    member_no: int | None = None

    def __post_init__(self) -> None:
        if self.tenant_id <= 0 or self.member_id <= 0 or self.user_id <= 0:
            raise ValueError("TenantContext 的租户、成员、用户标识必须为正整数")
        if not self.request_id:
            object.__setattr__(self, "request_id", get_request_id())


def set_tenant_context(context: TenantContext) -> Token[TenantContext | None]:
    return _tenant_context_var.set(context)


def reset_tenant_context(token: Token[TenantContext | None]) -> None:
    _tenant_context_var.reset(token)


def get_tenant_context(required: bool = True) -> TenantContext | None:
    context = _tenant_context_var.get()
    if context is None and required:
        raise AppError(ErrorCode.TENANT_CONTEXT_MISSING)
    return context


def require_tenant_context() -> TenantContext:
    context = _tenant_context_var.get()
    if context is None:
        raise AppError(ErrorCode.TENANT_CONTEXT_MISSING)
    return context
