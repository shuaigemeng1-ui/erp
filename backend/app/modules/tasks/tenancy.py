"""Worker / 调度进程的租户上下文与连接管理。

约束（见 plan.txt §2「租户上下文与连接管理」、§4「库存与任务可靠性」）：

* Worker 任务不信任消息体里的租户信息：只接受 ``tenant_id``，连接路由仍然只从中心库
  受控映射读取（``identity.service.load_tenant_db_route``）。
* 每个任务独立创建、关闭 Session，并用 ``app.core.context`` 的 ContextVar 重建
  ``TenantContext``；任务结束必须复位，绝不放模块级全局变量。
* 手动任务使用真实成员的授权快照，并在执行前用 ``app.api.deps.ensure_permission``
  复核（成员被停用或权限被撤销后，尚未执行的手动任务不能越权）。
* 定时 / 回调任务使用「限定能力的自动化身份」：只授予任务必需的功能权限与全部数据范围，
  不继承任何成员角色。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.access import ScopeGrant
from app.core.context import (
    TenantContext,
    TenantDbRoute,
    reset_tenant_context,
    set_tenant_context,
)
from app.core.errors import AppError, ErrorCode
from app.core.permissions import SCOPE_ALL
from app.db.session import center_session, tenant_session

logger = logging.getLogger("ventasync.tasks.tenancy")

# 自动化身份的能力上限：只覆盖任务本身需要的读取与同步动作，不含成员 / 角色 / 审计管理
AUTOMATION_PERMISSIONS: frozenset[str] = frozenset(
    {
        "order.read",
        "order.sync",
        "inventory.read",
        "stock.push",
        "channel.listing.read",
        "channel.listing.map",
        "store.read",
        "task.read",
    }
)


@dataclass(frozen=True, slots=True)
class JobIdentity:
    """一次后台任务的身份信息（由中心库成员关系得出）。"""

    tenant_id: int
    tenant_code: str
    tenant_name: str
    tenant_status: str
    timezone: str
    member_id: int
    member_status: str
    user_id: int
    is_automation: bool


def active_tenant_ids() -> list[int]:
    """中心库中状态为 active 的租户 ID 列表（调度 / Outbox 扫描用）。"""
    with center_session() as center:
        rows = center.execute(
            text("SELECT id FROM tenants WHERE status = 'active' ORDER BY id")
        ).all()
    return [int(row[0]) for row in rows]


def load_tenant_route(center: Session, tenant_id: int) -> TenantDbRoute:
    """从中心库受控映射读取租户库连接路由（延迟 import identity 契约）。"""
    from app.modules.identity.service import load_tenant_db_route

    route = load_tenant_db_route(center, tenant_id)
    if route is None:
        raise AppError(ErrorCode.TENANT_DATABASE_UNAVAILABLE)
    return route


def load_tenant_owner_user_id(center: Session, tenant_id: int) -> int | None:
    """读取租户负责人用户 ID（自动化身份的记账主体）。"""
    row = center.execute(
        text("SELECT owner_user_id FROM tenants WHERE id = :tenant_id"),
        {"tenant_id": tenant_id},
    ).first()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def resolve_job_identity(
    tenant_id: int,
    *,
    user_id: int | None,
    automation: bool,
) -> JobIdentity:
    """确定任务执行身份：手动任务用发起人，自动化任务用租户负责人但限定能力。

    统一在此执行「实例 / 租户绑定 + 租户 active + 部署模式」校验：任一不满足即拒绝并
    写审计（永久错误，不重试）。所有 Worker 处理器都经过本函数，因此不会绕过绑定。
    """
    from app.modules.identity.service import load_tenant_access
    from app.modules.tasks import instance

    with center_session() as center:
        decision = instance.evaluate_dispatch(
            instance.current_binding(), instance.load_tenant_deployment(center, int(tenant_id))
        )
        if not decision.allowed:
            logger.warning(
                "拒绝后台任务 tenant=%s reason=%s binding=%s decision=%s",
                tenant_id,
                decision.reason,
                instance.current_binding().describe(),
                decision.describe(),
            )
            if decision.audit_required:
                instance.audit_rejection(
                    int(tenant_id),
                    reason=decision.reason,
                    summary=f"后台任务实例绑定校验拒绝：{decision.reason}",
                    extra=decision.describe(),
                )
            raise AppError(
                ErrorCode.TENANT_FORBIDDEN,
                f"租户不满足本实例调度条件：{decision.reason}",
                details={"reason": decision.reason},
            )
        # 校验租户库路由存在（凭据只从中心库映射读取）
        load_tenant_route(center, tenant_id)
        actor_user_id = user_id
        if actor_user_id is None and not automation:
            raise AppError(ErrorCode.PERMISSION_DENIED, "手动任务缺少原始发起人")
        if actor_user_id is None:
            actor_user_id = load_tenant_owner_user_id(center, tenant_id)
        if not actor_user_id:
            raise AppError(
                ErrorCode.TENANT_FORBIDDEN,
                "任务缺少可用的执行身份（租户负责人未设置）",
            )
        access = load_tenant_access(center, tenant_id, actor_user_id)
        if access is None:
            raise AppError(ErrorCode.TENANT_FORBIDDEN)
        if access.tenant_status != "active":
            raise AppError(ErrorCode.TENANT_NOT_ACTIVE)
        if access.member_status != "active":
            raise AppError(ErrorCode.MEMBER_DISABLED)
        return JobIdentity(
            tenant_id=access.tenant_id,
            tenant_code=access.tenant_code,
            tenant_name=access.tenant_name,
            tenant_status=access.tenant_status,
            timezone=access.timezone,
            member_id=access.member_id,
            member_status=access.member_status,
            user_id=int(actor_user_id),
            is_automation=automation,
        )


def _automation_grants() -> tuple[ScopeGrant, ...]:
    """自动化身份的数据范围固定为租户全部（能力由权限集合限制）。"""
    return (
        ScopeGrant(
            role_binding_id=0,
            role_id=0,
            scope_type=SCOPE_ALL,
            permissions=AUTOMATION_PERMISSIONS,
        ),
    )


def build_tenant_context(
    db: Session,
    identity: JobIdentity,
    route: TenantDbRoute,
) -> TenantContext:
    """在已打开租户库会话的基础上构造不可变 ``TenantContext``。"""
    if identity.is_automation:
        return TenantContext(
            tenant_id=identity.tenant_id,
            tenant_code=identity.tenant_code,
            tenant_name=identity.tenant_name,
            tenant_status=identity.tenant_status,
            timezone=identity.timezone,
            member_id=identity.member_id,
            member_status=identity.member_status,
            user_id=identity.user_id,
            db_route=route,
            permission_version=0,
            permissions=AUTOMATION_PERMISSIONS,
            scope_grants=_automation_grants(),
            is_automation=True,
            actor_id=None,
        )

    from app.modules.access.service import load_access_snapshot

    snapshot = load_access_snapshot(db, identity.tenant_id, identity.member_id)
    return TenantContext(
        tenant_id=identity.tenant_id,
        tenant_code=identity.tenant_code,
        tenant_name=identity.tenant_name,
        tenant_status=identity.tenant_status,
        timezone=identity.timezone,
        member_id=identity.member_id,
        member_status=identity.member_status,
        user_id=identity.user_id,
        db_route=route,
        permission_version=snapshot.permission_version,
        permissions=snapshot.effective.permissions,
        scope_grants=snapshot.effective.grants,
        is_automation=False,
        actor_id=identity.user_id,
    )


@contextmanager
def job_tenant_session(
    tenant_id: int,
    *,
    user_id: int | None = None,
    automation: bool = True,
) -> Iterator[tuple[TenantContext, Session]]:
    """后台任务租户上下文：重建 ``TenantContext`` 并打开租户库会话。

    用法::

        with job_tenant_session(tenant_id, user_id=requested_by, automation=False) as (ctx, db):
            ...
            db.commit()

    退出时复位 ContextVar；未提交的事务由 Session 关闭时回滚（不会半途提交）。
    """
    identity = resolve_job_identity(tenant_id, user_id=user_id, automation=automation)
    with center_session() as center:
        route = load_tenant_route(center, tenant_id)
    with tenant_session(route) as db:
        context = build_tenant_context(db, identity, route)
        token = set_tenant_context(context)
        try:
            yield context, db
        finally:
            reset_tenant_context(token)


def guard_manual_permission(context: TenantContext, permission: str) -> None:
    """手动任务执行前复核成员权限（成员停用 / 权限撤销后立即失效）。"""
    if context.is_automation:
        return
    from app.api.deps import ensure_permission

    ensure_permission(
        context.user_id,
        context.tenant_id,
        permission,
        member_id=context.member_id,
    )
