"""FastAPI 依赖：会话、租户上下文、权限检查、数据库会话。

依赖顺序固定：请求中间件 -> 登录用户 -> 租户上下文（校验成员关系与租户状态）
-> 租户库会话 -> 权限依赖 -> 路由处理。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, Path, Request
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedUser, TenantAccessInfo
from app.core.context import (
    TenantContext,
    reset_tenant_context,
    set_tenant_context,
)
from app.core.errors import AppError, ErrorCode
from app.db.session import center_session, tenant_session
from app.modules.access.service import (
    AccessSnapshot,
    load_access_snapshot,
)
from app.modules.identity.service import (
    load_authenticated_user,
    load_tenant_access,
    load_tenant_db_route,
)


# --------------------------------------------------------------------------- 中心库
def get_center_db() -> Iterator[Session]:
    with center_session() as session:
        yield session


CenterDb = Annotated[Session, Depends(get_center_db)]


# --------------------------------------------------------------------------- 登录用户
def current_user(request: Request, center: CenterDb) -> AuthenticatedUser:
    payload = getattr(request.state, "session_payload", None)
    if not payload:
        raise AppError(ErrorCode.AUTH_SESSION_REQUIRED)
    user = load_authenticated_user(center, int(payload["user_id"]))
    if user is None:
        raise AppError(ErrorCode.AUTH_SESSION_REQUIRED)
    if user.status != "active":
        raise AppError(ErrorCode.AUTH_ACCOUNT_DISABLED)
    request.state.current_user = user
    return user


CurrentUser = Annotated[AuthenticatedUser, Depends(current_user)]


def optional_user(request: Request, center: CenterDb) -> AuthenticatedUser | None:
    payload = getattr(request.state, "session_payload", None)
    if not payload:
        return None
    user = load_authenticated_user(center, int(payload["user_id"]))
    if user is None or user.status != "active":
        return None
    return user


OptionalUser = Annotated[AuthenticatedUser | None, Depends(optional_user)]


def platform_admin(user: CurrentUser) -> AuthenticatedUser:
    if not user.is_platform_admin:
        raise AppError(ErrorCode.PLATFORM_PERMISSION_REQUIRED)
    return user


PlatformAdmin = Annotated[AuthenticatedUser, Depends(platform_admin)]


# --------------------------------------------------------------------------- 租户上下文
@contextmanager
def _tenant_dependency(
    request: Request, tenant_id: int, user: AuthenticatedUser
) -> Iterator[tuple[TenantContext, AccessSnapshot]]:
    with center_session() as center:
        access: TenantAccessInfo | None = load_tenant_access(center, tenant_id, user.user_id)
        if access is None:
            # 成员关系不存在与租户不存在返回同一结果，避免探测其他租户
            raise AppError(ErrorCode.TENANT_NOT_FOUND)
        if access.tenant_status in {"suspended", "provision_failed"}:
            raise AppError(ErrorCode.TENANT_SUSPENDED)
        if access.tenant_status != "active":
            raise AppError(ErrorCode.TENANT_NOT_ACTIVE)
        if access.member_status != "active":
            raise AppError(ErrorCode.MEMBER_DISABLED)
        route = load_tenant_db_route(center, tenant_id)
        if route is None:
            raise AppError(ErrorCode.TENANT_DATABASE_UNAVAILABLE)

    with tenant_session(route) as db:
        snapshot = load_access_snapshot(db, tenant_id, access.member_id)
        context = TenantContext(
            tenant_id=access.tenant_id,
            tenant_code=access.tenant_code,
            tenant_name=access.tenant_name,
            tenant_status=access.tenant_status,
            timezone=access.timezone,
            member_id=access.member_id,
            member_status=access.member_status,
            user_id=user.user_id,
            db_route=route,
            permission_version=snapshot.permission_version,
            permissions=snapshot.effective.permissions,
            scope_grants=snapshot.effective.grants,
            is_automation=False,
            actor_id=user.user_id,
            feature_flags=snapshot.feature_flags,
        )
        request.state.tenant_db = db
        request.state.tenant_access = access
        request.state.access_snapshot = snapshot
        token = set_tenant_context(context)
        try:
            yield context, snapshot
        finally:
            reset_tenant_context(token)
            request.state.tenant_db = None


def tenant_context(request: Request, user: CurrentUser, tenant_id: int = Path(ge=1)) -> Iterator[TenantContext]:
    with _tenant_dependency(request, tenant_id, user) as (context, _):
        yield context


TenantCtx = Annotated[TenantContext, Depends(tenant_context)]


def get_tenant_db(request: Request) -> Session:
    db = getattr(request.state, "tenant_db", None)
    if db is None:
        raise AppError(ErrorCode.TENANT_CONTEXT_MISSING)
    return db


TenantDb = Annotated[Session, Depends(get_tenant_db)]


def access_snapshot(request: Request) -> AccessSnapshot:
    snapshot = getattr(request.state, "access_snapshot", None)
    if snapshot is None:
        raise AppError(ErrorCode.TENANT_CONTEXT_MISSING)
    return snapshot


AccessSnapshotDep = Annotated[AccessSnapshot, Depends(access_snapshot)]


# --------------------------------------------------------------------------- 权限
class RequirePermission:
    """路由级功能权限校验。数据范围由 Service 层按目标对象校验。"""

    def __init__(self, permission: str) -> None:
        self.permission = permission

    def __call__(self, snapshot: AccessSnapshotDep) -> AccessSnapshot:
        if not snapshot.effective.has(self.permission):
            raise AppError(ErrorCode.PERMISSION_DENIED)
        return snapshot


def require_permission(permission: str) -> RequirePermission:
    return RequirePermission(permission)


def require_any_permission(*permissions: str) -> "RequireAnyPermission":
    return RequireAnyPermission(permissions)


class RequireAnyPermission:
    def __init__(self, permissions: tuple[str, ...]) -> None:
        self.permissions = permissions

    def __call__(self, snapshot: AccessSnapshotDep) -> AccessSnapshot:
        if not any(snapshot.effective.has(item) for item in self.permissions):
            raise AppError(ErrorCode.PERMISSION_DENIED)
        return snapshot


def ensure_permission(user_id: int, tenant_id: int, permission: str, *, member_id: int) -> None:
    """Worker / 手动任务执行前复核权限（见 plan.txt §4「手动任务执行前复核成员权限」）。"""
    with center_session() as center:
        access = load_tenant_access(center, tenant_id, user_id)
        if access is None:
            raise AppError(ErrorCode.TENANT_FORBIDDEN)
        if access.tenant_status != "active" or access.member_status != "active":
            raise AppError(ErrorCode.PERMISSION_DENIED)
        route = load_tenant_db_route(center, tenant_id)
        if route is None:
            raise AppError(ErrorCode.TENANT_DATABASE_UNAVAILABLE)
    with tenant_session(route) as db:
        snapshot = load_access_snapshot(db, tenant_id, member_id)
        if not snapshot.effective.has(permission):
            raise AppError(ErrorCode.PERMISSION_DENIED)
