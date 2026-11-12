"""身份模块查询层：只做 SQLAlchemy 查询，不写业务规则、不开事务。

中心库查询统一传 ``center`` Session；租户库查询统一传 ``db`` Session，
并且每个租户库查询必须带 ``tenant_id`` 条件。
"""

from __future__ import annotations

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.modules.identity.models_center import (
    PlatformRole,
    PlatformUserRole,
    ProvisioningJob,
    Tenant,
    TenantDatabase,
    TenantInvitation,
    TenantMembership,
    User,
)
from app.modules.identity.models_tenant import MemberRole, Role, RolePermission


# --------------------------------------------------------------------------- 用户
def get_user(center: Session, user_id: int) -> User | None:
    return center.execute(select(User).where(User.id == user_id)).scalar_one_or_none()


def get_user_by_email(center: Session, email: str) -> User | None:
    return center.execute(select(User).where(User.email == email)).scalar_one_or_none()


def platform_role_codes(center: Session, user_id: int) -> list[str]:
    rows = center.execute(
        select(PlatformRole.code)
        .join(PlatformUserRole, PlatformUserRole.role_id == PlatformRole.id)
        .where(PlatformUserRole.user_id == user_id)
    ).scalars()
    return [str(code) for code in rows]


def get_platform_role_by_code(center: Session, code: str) -> PlatformRole | None:
    return center.execute(select(PlatformRole).where(PlatformRole.code == code)).scalar_one_or_none()


# --------------------------------------------------------------------------- 租户
def get_tenant(center: Session, tenant_id: int) -> Tenant | None:
    return center.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()


def get_tenant_by_code(center: Session, code: str) -> Tenant | None:
    return center.execute(select(Tenant).where(Tenant.code == code)).scalar_one_or_none()


def list_tenants(
    center: Session,
    *,
    status: str | None = None,
    keyword: str = "",
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[Tenant], int]:
    stmt: Select[tuple[Tenant]] = select(Tenant)
    if status:
        stmt = stmt.where(Tenant.status == status)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(Tenant.name.like(like), Tenant.code.like(like)))
    total = int(center.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    rows = center.execute(stmt.order_by(Tenant.id.desc()).offset(offset).limit(limit)).scalars().all()
    return list(rows), total


# --------------------------------------------------------------------------- 成员关系
def get_membership(center: Session, tenant_id: int, user_id: int) -> TenantMembership | None:
    return center.execute(
        select(TenantMembership).where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.user_id == user_id,
        )
    ).scalar_one_or_none()


def get_membership_by_id(center: Session, tenant_id: int, membership_id: int) -> TenantMembership | None:
    return center.execute(
        select(TenantMembership).where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.id == membership_id,
        )
    ).scalar_one_or_none()


def list_user_memberships(center: Session, user_id: int) -> list[tuple[TenantMembership, Tenant]]:
    rows = center.execute(
        select(TenantMembership, Tenant)
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .where(TenantMembership.user_id == user_id)
        .order_by(Tenant.id)
    ).all()
    return [(row[0], row[1]) for row in rows]


def list_tenant_memberships(
    center: Session,
    tenant_id: int,
    *,
    status: str | None = None,
    keyword: str = "",
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[tuple[TenantMembership, User]], int]:
    stmt = (
        select(TenantMembership, User)
        .join(User, User.id == TenantMembership.user_id)
        .where(TenantMembership.tenant_id == tenant_id)
    )
    if status:
        stmt = stmt.where(TenantMembership.status == status)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(User.email.like(like), User.display_name.like(like)))
    total = int(center.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    rows = center.execute(stmt.order_by(TenantMembership.id).offset(offset).limit(limit)).all()
    return [(row[0], row[1]) for row in rows], total


def count_active_owners(center: Session, tenant_id: int) -> int:
    return int(
        center.execute(
            select(func.count())
            .select_from(TenantMembership)
            .where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.is_owner.is_(True),
                TenantMembership.status == "active",
            )
        ).scalar_one()
    )


def list_owner_memberships(center: Session, tenant_id: int) -> list[TenantMembership]:
    """租户当前所有者成员（含非 active，用于展示与转移校验）。"""
    return list(
        center.execute(
            select(TenantMembership)
            .where(TenantMembership.tenant_id == tenant_id, TenantMembership.is_owner.is_(True))
            .order_by(TenantMembership.id)
        ).scalars()
    )


# --------------------------------------------------------------------------- 数据库路由与开通任务
def get_tenant_database(center: Session, tenant_id: int) -> TenantDatabase | None:
    return center.execute(
        select(TenantDatabase).where(TenantDatabase.tenant_id == tenant_id)
    ).scalar_one_or_none()


def list_provisioning_jobs(center: Session, tenant_id: int) -> list[ProvisioningJob]:
    return list(
        center.execute(
            select(ProvisioningJob)
            .where(ProvisioningJob.tenant_id == tenant_id)
            .order_by(ProvisioningJob.id)
        ).scalars()
    )


def get_provisioning_job(center: Session, job_id: int) -> ProvisioningJob | None:
    return center.execute(
        select(ProvisioningJob).where(ProvisioningJob.id == job_id)
    ).scalar_one_or_none()


def get_latest_provisioning_job(center: Session, tenant_id: int, step: str) -> ProvisioningJob | None:
    return center.execute(
        select(ProvisioningJob)
        .where(ProvisioningJob.tenant_id == tenant_id, ProvisioningJob.step == step)
        .order_by(ProvisioningJob.id.desc())
        .limit(1)
    ).scalar_one_or_none()


# --------------------------------------------------------------------------- 邀请
def get_invitation_by_token_hash(center: Session, token_hash: str) -> TenantInvitation | None:
    return center.execute(
        select(TenantInvitation).where(TenantInvitation.token_hash == token_hash)
    ).scalar_one_or_none()


def list_pending_invitations(
    center: Session, tenant_id: int, email: str
) -> list[TenantInvitation]:
    return list(
        center.execute(
            select(TenantInvitation).where(
                TenantInvitation.tenant_id == tenant_id,
                TenantInvitation.email == email,
                TenantInvitation.status == "pending",
            )
        ).scalars()
    )


def get_invitation(center: Session, tenant_id: int, invitation_id: int) -> TenantInvitation | None:
    return center.execute(
        select(TenantInvitation).where(
            TenantInvitation.tenant_id == tenant_id,
            TenantInvitation.id == invitation_id,
        )
    ).scalar_one_or_none()


def get_latest_invitation(center: Session, tenant_id: int, email: str) -> TenantInvitation | None:
    """某邮箱最近一条邀请（不限状态，供详情展示与重新签发）。"""
    return center.execute(
        select(TenantInvitation)
        .where(
            TenantInvitation.tenant_id == tenant_id,
            TenantInvitation.email == email,
        )
        .order_by(TenantInvitation.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def list_invitations(center: Session, tenant_id: int, *, offset: int = 0, limit: int = 20) -> list[TenantInvitation]:
    return list(
        center.execute(
            select(TenantInvitation)
            .where(TenantInvitation.tenant_id == tenant_id)
            .order_by(TenantInvitation.id.desc())
            .offset(offset)
            .limit(limit)
        ).scalars()
    )


# --------------------------------------------------------------------------- 租户库：角色
def list_roles(db: Session, tenant_id: int, *, include_disabled: bool = False) -> list[Role]:
    stmt = select(Role).where(Role.tenant_id == tenant_id)
    if not include_disabled:
        stmt = stmt.where(Role.status == "active")
    return list(db.execute(stmt.order_by(Role.is_system.desc(), Role.id)).scalars())


def get_role(db: Session, tenant_id: int, role_id: int) -> Role | None:
    return db.execute(
        select(Role).where(Role.tenant_id == tenant_id, Role.id == role_id)
    ).scalar_one_or_none()


def get_role_by_code(db: Session, tenant_id: int, code: str) -> Role | None:
    return db.execute(
        select(Role).where(Role.tenant_id == tenant_id, Role.code == code)
    ).scalar_one_or_none()


def role_permission_map(
    db: Session, tenant_id: int, role_ids: list[int], *, lock: bool = False,
) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {}
    if not role_ids:
        return result
    statement = (
        select(RolePermission.role_id, RolePermission.permission_code).where(
            RolePermission.tenant_id == tenant_id,
            RolePermission.role_id.in_(role_ids),
        )
        .order_by(RolePermission.permission_code)
    )
    if lock:
        statement = statement.with_for_update()
    rows = db.execute(statement).all()
    for role_id, permission_code in rows:
        result.setdefault(int(role_id), []).append(str(permission_code))
    return result


def list_member_role_rows(
    db: Session, tenant_id: int, member_ids: list[int]
) -> list[tuple[int, int, int]]:
    """返回 ``[(member_id, member_role_id, role_id)]``，用于成员列表与 bootstrap。"""
    if not member_ids:
        return []
    rows = db.execute(
        select(MemberRole.member_id, MemberRole.id, MemberRole.role_id)
        .join(Role, (Role.id == MemberRole.role_id) & (Role.tenant_id == MemberRole.tenant_id))
        .where(
            MemberRole.tenant_id == tenant_id,
            MemberRole.member_id.in_(member_ids),
        )
        .order_by(MemberRole.id)
    ).all()
    return [(int(row[0]), int(row[1]), int(row[2])) for row in rows]


def count_role_bindings(db: Session, tenant_id: int, role_id: int) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(MemberRole)
            .where(MemberRole.tenant_id == tenant_id, MemberRole.role_id == role_id)
        ).scalar_one()
    )


def list_role_binding_scopes(
    db: Session, tenant_id: int, member_role_ids: list[int]
) -> dict[int, list[tuple[str, int]]]:
    from app.modules.access.models_tenant import RoleBindingScope

    result: dict[int, list[tuple[str, int]]] = {}
    if not member_role_ids:
        return result
    rows = db.execute(
        select(
            RoleBindingScope.member_role_id,
            RoleBindingScope.scope_type,
            RoleBindingScope.scope_id,
        )
        .where(
            RoleBindingScope.tenant_id == tenant_id,
            RoleBindingScope.member_role_id.in_(member_role_ids),
        )
        .order_by(RoleBindingScope.scope_type, RoleBindingScope.scope_id)
    ).all()
    for member_role_id, scope_type, scope_id in rows:
        result.setdefault(int(member_role_id), []).append((str(scope_type), int(scope_id)))
    return result
