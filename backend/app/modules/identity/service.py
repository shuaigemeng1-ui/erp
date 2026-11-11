"""身份与租户服务：登录用户、成员关系、邀请、成员与角色管理。

固定契约（``app/api/deps.py`` 依赖，签名不得修改，见 docs/architecture.md）：

* ``load_authenticated_user(center, user_id) -> AuthenticatedUser | None``
* ``load_tenant_access(center, tenant_id, user_id) -> TenantAccessInfo | None``
* ``load_tenant_db_route(center, tenant_id) -> TenantDbRoute | None``
* ``is_platform_admin(center, user_id) -> bool``

业务规则（见 plan.txt §2-§3）：

* 邀请令牌只存 SHA-256 摘要、有时效、只能使用一次；
* 角色绑定写入前校验成员确属当前租户，并做防提权校验（不得授予操作者自身没有的权限）；
* 权限 / 绑定 / 成员状态变更后递增权限版本，并写租户审计；
* 停用成员、修改密码、退出登录立即撤销会话。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.access import EffectivePermissions, ScopeGrant
from app.core.auth import AuthenticatedUser, TenantAccessInfo
from app.core.config import settings
from app.core.context import TenantDbRoute, TenantContext
from app.core.errors import AppError, ErrorCode
from app.core.permissions import ALL_PERMISSIONS, PREDEFINED_ROLE_PERMISSIONS, Permission
from app.core.redis import revoke_user_sessions
from app.core.security import (
    hash_password,
    random_token,
    sha256_hex,
    utc_after,
    utc_now,
    validate_password_strength,
    verify_password,
)
from app.db.session import tenant_session
from app.modules.access.service import (
    AccessSnapshot,
    assert_can_grant,
    assert_can_manage_permissions,
    bump_permission_version,
    feature_flag_map,
    validate_scope_choice,
)
from app.modules.audit.service import record as record_audit
from app.modules.identity import rate_limit
from app.modules.identity import repository as repo
from app.modules.identity.constants import (
    DEFAULT_STOCK_PLACE_CODE,
    InvitationStatus,
    MembershipStatus,
    PLATFORM_ROLE_ADMIN,
    TenantStatus,
    UserStatus,
)
from app.modules.identity.models_center import Tenant, TenantInvitation, TenantMembership, User
from app.modules.identity.models_tenant import MemberRole, Role, RolePermission

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- 输入结构
@dataclass(frozen=True, slots=True)
class ScopeChoice:
    """一个数据范围选择。"""

    scope_type: str
    scope_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class RoleBindingChoice:
    """一条角色绑定：角色 + 数据范围（空范围代表没有授权，会被拒绝）。"""

    role_id: int
    scopes: tuple[ScopeChoice, ...] = ()


# --------------------------------------------------------------------------- 通用工具
@contextmanager
def run_in_transaction(db: Session) -> Iterator[Session]:
    """Service 事务边界：成功提交，失败回滚。"""
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def iso_utc(value: datetime | None) -> str | None:
    """时间统一按 UTC ISO 返回（数据库存 naive UTC）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def user_payload(user: User, *, is_platform_admin: bool) -> dict[str, Any]:
    """``/me`` 与登录响应中的用户结构。"""
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "status": user.status,
        "is_platform_admin": is_platform_admin,
        "locale": user.locale,
    }


def _token_hash(token: str) -> str:
    return sha256_hex(token)


# --------------------------------------------------------------------------- 固定契约
def load_authenticated_user(center: Session, user_id: int) -> AuthenticatedUser | None:
    """加载登录用户（含是否平台管理员）。用户不存在返回 ``None``。"""
    user = repo.get_user(center, user_id)
    if user is None:
        return None
    return AuthenticatedUser(
        user_id=int(user.id),
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        is_platform_admin=is_platform_admin(center, user_id),
        locale=user.locale or "zh-CN",
    )


def load_tenant_access(center: Session, tenant_id: int, user_id: int) -> TenantAccessInfo | None:
    """校验用户与租户成员关系；租户或成员关系不存在返回 ``None``。

    成员关系不存在与租户不存在返回同一结果，避免通过接口探测其他租户。
    """
    tenant = repo.get_tenant(center, tenant_id)
    if tenant is None:
        return None
    membership = repo.get_membership(center, tenant_id, user_id)
    if membership is None:
        return None
    return TenantAccessInfo(
        tenant_id=int(tenant.id),
        tenant_code=tenant.code,
        tenant_name=tenant.name,
        tenant_status=tenant.status,
        timezone=tenant.timezone or "Asia/Shanghai",
        member_id=int(membership.id),
        member_status=membership.status,
        role_summary=(),
    )


def load_tenant_db_route(center: Session, tenant_id: int) -> TenantDbRoute | None:
    """从中心库受控映射读取租户库连接信息（含加密凭据解密）。"""
    from app.core.security import decrypt_secret

    database = repo.get_tenant_database(center, tenant_id)
    if database is None or database.status != "ready":
        return None
    try:
        password = decrypt_secret(database.password_cipher)
    except Exception as exc:  # noqa: BLE001 - 密钥轮换或密文损坏统一按不可用处理
        logger.error("租户库凭据解密失败 tenant=%s: %s", tenant_id, exc)
        raise AppError(ErrorCode.TENANT_DATABASE_UNAVAILABLE, "租户数据库凭据不可用") from exc
    return TenantDbRoute(
        tenant_id=int(database.tenant_id),
        host=database.host,
        port=int(database.port),
        database=database.database_name,
        username=database.username,
        password=password,
        credentials_version=int(database.credentials_version or 1),
    )


def is_platform_admin(center: Session, user_id: int) -> bool:
    """是否拥有平台管理员角色。"""
    return PLATFORM_ROLE_ADMIN in repo.platform_role_codes(center, user_id)


# --------------------------------------------------------------------------- 登录
def authenticate(center: Session, email: str, password: str, *, ip: str = "") -> User:
    """校验邮箱与密码；失败抛 ``AUTH_INVALID_CREDENTIALS``，停用抛 ``AUTH_ACCOUNT_DISABLED``。

    连续失败达到阈值（同一邮箱 + IP，15 分钟内 5 次）抛 ``AUTH_RATE_LIMITED``；
    成功登录清零计数。限流组件故障时降级放行。
    """
    normalized = normalize_email(email)
    rate_limit.ensure_login_allowed(normalized, ip)
    user = repo.get_user_by_email(center, normalized)
    if user is None or not user.password_hash or not verify_password(password, user.password_hash):
        rate_limit.record_login_failure(normalized, ip)
        raise AppError(ErrorCode.AUTH_INVALID_CREDENTIALS)
    if user.status != UserStatus.ACTIVE.value:
        raise AppError(ErrorCode.AUTH_ACCOUNT_DISABLED)
    rate_limit.clear_login_failures(normalized, ip)
    return user


def record_login(center: Session, user: User) -> None:
    """记录最近登录时间（会话由 core.redis 维护，不在这里处理）。"""
    with run_in_transaction(center):
        user.last_login_at = utc_now()
        center.add(user)


def change_password(center: Session, user_id: int, current_password: str, new_password: str) -> None:
    """修改密码：校验原密码、强度，成功后撤销该用户全部会话。"""
    user = repo.get_user(center, user_id)
    if user is None:
        raise AppError(ErrorCode.AUTH_SESSION_REQUIRED)
    if not user.password_hash or not verify_password(current_password, user.password_hash):
        raise AppError(ErrorCode.AUTH_INVALID_CREDENTIALS, "当前密码不正确")
    validate_password_strength(new_password)
    with run_in_transaction(center):
        user.password_hash = hash_password(new_password)
        center.add(user)
    revoke_user_sessions(int(user.id))


def list_user_tenants(center: Session, user_id: int) -> list[dict[str, Any]]:
    """当前账号可访问的租户（含成员状态与角色编码）。

    单个租户库不可用时只跳过其角色，不影响其它租户的展示。
    """
    result: list[dict[str, Any]] = []
    for membership, tenant in repo.list_user_memberships(center, user_id):
        role_codes: list[str] = []
        if membership.status == MembershipStatus.ACTIVE.value:
            role_codes = _member_role_codes(center, int(tenant.id), int(membership.id))
        result.append(
            {
                "id": str(tenant.id),
                "code": tenant.code,
                "name": tenant.name,
                "status": tenant.status,
                "timezone": tenant.timezone,
                "member_status": membership.status,
                "is_owner": bool(membership.is_owner),
                "roles": role_codes,
            }
        )
    return result


def _member_role_codes(center: Session, tenant_id: int, member_id: int) -> list[str]:
    route = load_tenant_db_route(center, tenant_id)
    if route is None:
        return []
    try:
        with tenant_session(route) as db:
            row_pairs = repo.list_member_role_rows(db, tenant_id, [member_id])
            role_ids = sorted({role_id for _, _, role_id in row_pairs})
            roles = repo.list_roles(db, tenant_id, include_disabled=True)
            codes_by_id = {int(role.id): role.code for role in roles if int(role.id) in role_ids}
            return [codes_by_id[role_id] for role_id in role_ids if role_id in codes_by_id]
    except Exception as exc:  # noqa: BLE001 - 租户库异常不影响 /me/tenants
        logger.warning("读取租户 %s 成员角色失败: %s", tenant_id, exc)
        return []


# --------------------------------------------------------------------------- 邀请
def _invitation_expired(invitation: TenantInvitation) -> bool:
    return invitation.expires_at <= utc_now()


def _load_invitation_by_token(center: Session, token: str) -> TenantInvitation:
    invitation = repo.get_invitation_by_token_hash(center, _token_hash(token))
    if invitation is None:
        raise AppError(ErrorCode.AUTH_TOKEN_INVALID)
    return invitation


def preview_invitation(center: Session, token: str) -> dict[str, Any]:
    """邀请信息预览：令牌无效 / 过期 / 已用返回对应错误码。"""
    invitation = _load_invitation_by_token(center, token)
    if invitation.status == InvitationStatus.ACCEPTED.value:
        raise AppError(ErrorCode.AUTH_TOKEN_USED)
    if invitation.status == InvitationStatus.REVOKED.value:
        raise AppError(ErrorCode.AUTH_TOKEN_INVALID)
    if invitation.status == InvitationStatus.EXPIRED.value or _invitation_expired(invitation):
        raise AppError(ErrorCode.AUTH_TOKEN_EXPIRED)
    tenant = repo.get_tenant(center, int(invitation.tenant_id))
    if tenant is None:
        raise AppError(ErrorCode.AUTH_TOKEN_INVALID)
    return {
        "tenant_id": str(tenant.id),
        "tenant_name": tenant.name,
        "email": invitation.email,
        "status": invitation.status,
        "expires_at": iso_utc(invitation.expires_at),
    }


def accept_invitation(
    center: Session, *, token: str, password: str, display_name: str = ""
) -> tuple[User, list[dict[str, Any]]]:
    """接受邀请并设置密码：一次有效，重复使用返回 ``AUTH_TOKEN_USED``。"""
    invitation = _load_invitation_by_token(center, token)
    if invitation.status == InvitationStatus.ACCEPTED.value:
        raise AppError(ErrorCode.AUTH_TOKEN_USED)
    if invitation.status == InvitationStatus.REVOKED.value:
        raise AppError(ErrorCode.AUTH_TOKEN_INVALID)
    if invitation.status == InvitationStatus.EXPIRED.value or _invitation_expired(invitation):
        with run_in_transaction(center):
            invitation.status = InvitationStatus.EXPIRED.value
            center.add(invitation)
        raise AppError(ErrorCode.AUTH_TOKEN_EXPIRED)

    tenant = repo.get_tenant(center, int(invitation.tenant_id))
    if tenant is None:
        raise AppError(ErrorCode.AUTH_TOKEN_INVALID)
    if tenant.status != TenantStatus.ACTIVE.value:
        # 租户尚未开通完成时不允许激活，避免半开通状态产生可用账号
        raise AppError(ErrorCode.TENANT_NOT_ACTIVE)

    validate_password_strength(password)
    email = normalize_email(invitation.email)
    user = repo.get_user_by_email(center, email)
    bindings = _bindings_from_json(invitation.role_ids)
    with run_in_transaction(center):
        if user is None:
            user = User(
                email=email,
                password_hash=hash_password(password),
                display_name=(display_name or email.split("@")[0])[:100],
                status=UserStatus.ACTIVE.value,
                locale="zh-CN",
            )
            center.add(user)
            center.flush()
        else:
            # 已有账号：只补空密码，不通过邀请覆盖既有密码（避免账号被邀请劫持）
            if not user.password_hash:
                user.password_hash = hash_password(password)
            if display_name and not user.display_name:
                user.display_name = display_name[:100]
            user.status = UserStatus.ACTIVE.value
            center.add(user)

        membership = repo.get_membership(center, int(tenant.id), int(user.id))
        if membership is None:
            membership = TenantMembership(
                tenant_id=int(tenant.id),
                user_id=int(user.id),
                status=MembershipStatus.ACTIVE.value,
                is_owner=False,
                joined_at=utc_now(),
            )
            center.add(membership)
            center.flush()
        else:
            membership.status = MembershipStatus.ACTIVE.value
            membership.joined_at = membership.joined_at or utc_now()
            center.add(membership)

        invitation.status = InvitationStatus.ACCEPTED.value
        invitation.accepted_at = utc_now()
        invitation.accepted_user_id = int(user.id)
        center.add(invitation)

    route = load_tenant_db_route(center, int(tenant.id))
    if route is None:
        logger.warning("租户 %s 缺少数据库路由，邀请已接受但未写入角色绑定", tenant.id)
    else:
        with tenant_session(route) as db, run_in_transaction(db):
            if bindings:
                _write_bindings(
                    db,
                    tenant_id=int(tenant.id),
                    member_id=int(membership.id),
                    bindings=bindings,
                    assigned_by=None,
                )
            else:
                # 邀请未预置角色时保留开通流程已写入的绑定，避免接受邀请后失去权限
                logger.info("邀请 %s 未预置角色绑定，保留既有绑定", invitation.id)
            bump_permission_version(db, int(tenant.id))
            record_audit(
                db,
                tenant_id=int(tenant.id),
                action="member.invitation.accept",
                resource_type="member",
                resource_id=int(membership.id),
                actor_id=int(user.id),
                summary=f"接受邀请加入租户：{user.email}",
                payload={"roles": [binding.role_id for binding in bindings]},
            )
    return user, list_user_tenants(center, int(user.id))


def create_member_invitation(
    center: Session,
    db: Session,
    *,
    tenant_id: int,
    email: str,
    bindings: list[RoleBindingChoice],
    actor_id: int,
    actor_effective: EffectivePermissions,
) -> dict[str, Any]:
    """邀请成员：校验角色与范围、防提权，令牌只返回一次且库中只存摘要。"""
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        raise AppError(ErrorCode.VALIDATION_ERROR, "邮箱格式不正确")
    validated = _validate_bindings(db, tenant_id, bindings)
    requested = _effective_for_bindings(db, tenant_id, validated)
    assert_can_grant(actor_effective, requested)

    with run_in_transaction(center):
        # 旧邀请全部置为撤销，保证同一邮箱同时只有一个可用邀请
        for stale in repo.list_pending_invitations(center, tenant_id, normalized):
            stale.status = InvitationStatus.REVOKED.value
            center.add(stale)
        user = repo.get_user_by_email(center, normalized)
        if user is None:
            user = User(
                email=normalized,
                password_hash=None,
                display_name=normalized.split("@")[0][:100],
                status=UserStatus.ACTIVE.value,
                locale="zh-CN",
            )
            center.add(user)
            center.flush()
        membership = repo.get_membership(center, tenant_id, int(user.id))
        if membership is None:
            membership = TenantMembership(
                tenant_id=tenant_id,
                user_id=int(user.id),
                status=MembershipStatus.INVITED.value,
                is_owner=False,
            )
            center.add(membership)
            center.flush()
        elif membership.status == MembershipStatus.ACTIVE.value:
            raise AppError(ErrorCode.CONFLICT, "该用户已是租户成员")
        else:
            membership.status = MembershipStatus.INVITED.value
            center.add(membership)

        token = random_token()
        invitation = TenantInvitation(
            tenant_id=tenant_id,
            email=normalized,
            token_hash=_token_hash(token),
            role_ids=_bindings_to_json(validated),
            status=InvitationStatus.PENDING.value,
            expires_at=utc_after(settings.invitation_ttl_seconds),
            invited_by=actor_id,
        )
        center.add(invitation)
        center.flush()
        invitation_id = int(invitation.id)
        expires_at = invitation.expires_at

    with run_in_transaction(db):
        record_audit(
            db,
            tenant_id=tenant_id,
            action="member.invite",
            resource_type="member",
            resource_id=invitation_id,
            actor_id=actor_id,
            summary=f"邀请成员：{normalized}",
            payload={"roles": [item.role_id for item in validated]},
        )
        bump_permission_version(db, tenant_id)
    return {
        "invitation_id": str(invitation_id),
        "email": normalized,
        "expires_at": iso_utc(expires_at),
        # 首版没有邮件通道：只在本响应返回一次原文，库中仅存摘要
        "invitation_token": token,
    }


def _bindings_to_json(bindings: list[RoleBindingChoice]) -> list[dict[str, Any]]:
    return [
        {
            "role_id": int(binding.role_id),
            "scopes": [
                {"scope_type": scope.scope_type, "scope_ids": list(scope.scope_ids)}
                for scope in binding.scopes
            ],
        }
        for binding in bindings
    ]


def _bindings_from_json(raw: Any) -> list[RoleBindingChoice]:
    """从邀请的 JSON 还原绑定；格式非法时按空绑定处理（不会越权）。"""
    result: list[RoleBindingChoice] = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            role_id = int(item["role_id"])
        except (KeyError, TypeError, ValueError):
            continue
        scopes: list[ScopeChoice] = []
        for scope in item.get("scopes") or []:
            if not isinstance(scope, dict):
                continue
            try:
                scope_type, scope_ids = validate_scope_choice(
                    str(scope.get("scope_type") or ""),
                    [int(value) for value in (scope.get("scope_ids") or [])],
                )
            except (AppError, TypeError, ValueError):
                continue
            scopes.append(ScopeChoice(scope_type=scope_type, scope_ids=tuple(scope_ids)))
        result.append(RoleBindingChoice(role_id=role_id, scopes=tuple(scopes)))
    return result


# --------------------------------------------------------------------------- 负责人邀请重签
INVITATION_ACTIVATION_PATH = "/activate/{token}"
OWNER_INVITATION_AUDIT_ACTION = "platform.tenant.owner_invitation"
RESEND_INVITATION_AUDIT_ACTION = "member.invitation.resend"


def invitation_url(token: str) -> str:
    """激活链接路径（前端路由 ``/activate/{token}``，绝对地址由部署域名拼接）。"""
    return INVITATION_ACTIVATION_PATH.format(token=token)


def invitation_payload(
    invitation: TenantInvitation, *, token: str | None = None
) -> dict[str, Any]:
    """邀请响应结构。

    只有轮换 / 新建令牌的那一次响应携带明文 ``token`` 与 ``invitation_url``；
    详情类接口不得调用本函数的 token 分支（库中只存 SHA-256 摘要）。
    """
    payload: dict[str, Any] = {
        "invitation_id": str(invitation.id),
        "email": invitation.email,
        "status": invitation.status,
        "expires_at": iso_utc(invitation.expires_at),
        "invited_at": iso_utc(invitation.created_at),
    }
    if token:
        payload["token"] = token
        payload["invitation_url"] = invitation_url(token)
    return payload


def _owner_email(center: Session, tenant: Tenant) -> str:
    if not tenant.owner_user_id:
        return ""
    owner = repo.get_user(center, int(tenant.owner_user_id))
    return owner.email if owner is not None else ""


def latest_owner_invitation(center: Session, tenant: Tenant) -> TenantInvitation | None:
    """租户负责人最近一条邀请（不限状态，供详情展示）。"""
    email = _owner_email(center, tenant)
    if not email:
        return None
    return repo.get_latest_invitation(center, int(tenant.id), email)


def _owner_role_bindings_json(db: Session, tenant_id: int) -> list[dict[str, Any]]:
    """负责人邀请预置的角色与数据范围（owner 角色 + 全部范围）。"""
    owner_role = _owner_role(db, tenant_id)
    if owner_role is None:
        raise AppError(ErrorCode.CONFLICT, "租户缺少所有者角色，请先完成租户开通")
    return [
        {
            "role_id": int(owner_role.id),
            "scopes": [{"scope_type": "all", "scope_ids": []}],
        }
    ]


def issue_owner_invitation(
    center: Session, tenant_id: int, *, actor_user_id: int | None = None
) -> dict[str, Any]:
    """重新签发租户负责人邀请（平台管理员，无邮件通道时由管理员手工交付链接）。

    * 旧 pending 邀请全部置 ``revoked``（保证同一邮箱只有一个可用令牌）；
    * 令牌只存 SHA-256 摘要，明文只在本次响应返回一次；
    * 负责人已激活时返回 ``409``，避免通过邀请重置既有账号。
    """
    from app.core.config import settings as app_settings

    tenant = repo.get_tenant(center, tenant_id)
    if tenant is None:
        raise AppError(ErrorCode.TENANT_NOT_FOUND)
    if tenant.status != TenantStatus.ACTIVE.value:
        raise AppError(ErrorCode.TENANT_NOT_ACTIVE, "租户尚未开通完成，无法签发负责人邀请")
    email = _owner_email(center, tenant)
    if not email:
        raise AppError(ErrorCode.CONFLICT, "租户缺少负责人，无法签发邀请")
    membership = repo.get_membership(center, tenant_id, int(tenant.owner_user_id or 0))
    if membership is None:
        raise AppError(ErrorCode.CONFLICT, "负责人成员关系不存在，无法签发邀请")
    if membership.status == MembershipStatus.ACTIVE.value:
        raise AppError(ErrorCode.CONFLICT, "负责人已激活，无需重新邀请")

    route = load_tenant_db_route(center, tenant_id)
    if route is None:
        raise AppError(ErrorCode.TENANT_DATABASE_UNAVAILABLE, "租户数据库不可用，无法签发邀请")
    with tenant_session(route) as db:
        role_bindings_json = _owner_role_bindings_json(db, tenant_id)

    token = random_token()
    with run_in_transaction(center):
        for stale in repo.list_pending_invitations(center, tenant_id, email):
            stale.status = InvitationStatus.REVOKED.value
            center.add(stale)
        if membership.status != MembershipStatus.INVITED.value:
            membership.status = MembershipStatus.INVITED.value
            center.add(membership)
        invitation = TenantInvitation(
            tenant_id=tenant_id,
            email=email,
            token_hash=_token_hash(token),
            role_ids=role_bindings_json,
            status=InvitationStatus.PENDING.value,
            expires_at=utc_after(app_settings.invitation_ttl_seconds),
            invited_by=actor_user_id,
        )
        center.add(invitation)
        center.flush()
        payload = invitation_payload(invitation, token=token)
    payload["membership_id"] = str(membership.id)
    return payload


def resend_invitation(
    center: Session,
    db: Session,
    *,
    tenant_id: int,
    invitation_id: int,
    actor_id: int,
) -> dict[str, Any]:
    """重新签发租户成员邀请（member.invite）：轮换令牌并写租户审计。"""
    from app.core.config import settings as app_settings

    tenant = repo.get_tenant(center, tenant_id)
    if tenant is None:
        raise AppError(ErrorCode.TENANT_NOT_FOUND)
    invitation = repo.get_invitation(center, tenant_id, invitation_id)
    if invitation is None:
        raise AppError(ErrorCode.NOT_FOUND, "邀请不存在")
    if invitation.status == InvitationStatus.ACCEPTED.value:
        raise AppError(ErrorCode.CONFLICT, "邀请已被接受，不能重新签发")
    email = invitation.email
    role_bindings_json = invitation.role_ids or []
    token = random_token()
    with run_in_transaction(center):
        for stale in repo.list_pending_invitations(center, tenant_id, email):
            stale.status = InvitationStatus.REVOKED.value
            center.add(stale)
        new_invitation = TenantInvitation(
            tenant_id=tenant_id,
            email=email,
            token_hash=_token_hash(token),
            role_ids=role_bindings_json,
            status=InvitationStatus.PENDING.value,
            expires_at=utc_after(app_settings.invitation_ttl_seconds),
            invited_by=actor_id,
        )
        center.add(new_invitation)
        center.flush()
        payload = invitation_payload(new_invitation, token=token)
    with run_in_transaction(db):
        bump_permission_version(db, tenant_id)
        record_audit(
            db,
            tenant_id=tenant_id,
            action=RESEND_INVITATION_AUDIT_ACTION,
            resource_type="invitation",
            resource_id=int(payload["invitation_id"]),
            actor_id=actor_id,
            summary=f"重新签发成员邀请：{email}",
            payload={"email": email, "previous_invitation_id": str(invitation_id)},
        )
    return payload


# --------------------------------------------------------------------------- 角色绑定
def _validate_bindings(
    db: Session, tenant_id: int, bindings: list[RoleBindingChoice]
) -> list[RoleBindingChoice]:
    """校验角色存在、范围合法；返回规范化后的绑定（去重、排序）。"""
    if not bindings:
        raise AppError(ErrorCode.VALIDATION_ERROR, "至少需要分配一个角色")
    roles = {int(role.id): role for role in repo.list_roles(db, tenant_id, include_disabled=True)}
    normalized: list[RoleBindingChoice] = []
    seen: set[int] = set()
    for binding in bindings:
        role = roles.get(int(binding.role_id))
        if role is None:
            raise AppError(ErrorCode.NOT_FOUND, "角色不存在", details={"role_id": str(binding.role_id)})
        if role.status != "active":
            raise AppError(ErrorCode.VALIDATION_ERROR, "角色已停用，无法分配")
        if int(binding.role_id) in seen:
            raise AppError(ErrorCode.VALIDATION_ERROR, "同一角色不能重复分配")
        seen.add(int(binding.role_id))
        if not binding.scopes:
            # 空范围代表没有授权，必须显式选择范围
            raise AppError(ErrorCode.VALIDATION_ERROR, "数据范围不能为空，请选择全部或指定对象")
        scopes: list[ScopeChoice] = []
        for scope in binding.scopes:
            scope_type, scope_ids = validate_scope_choice(scope.scope_type, list(scope.scope_ids))
            if any(item.scope_type == scope_type for item in scopes):
                raise AppError(ErrorCode.VALIDATION_ERROR, f"数据范围重复：{scope_type}")
            scopes.append(ScopeChoice(scope_type=scope_type, scope_ids=tuple(scope_ids)))
        normalized.append(RoleBindingChoice(role_id=int(binding.role_id), scopes=tuple(scopes)))
    return normalized


def _effective_for_bindings(
    db: Session, tenant_id: int, bindings: list[RoleBindingChoice]
) -> EffectivePermissions:
    """按待授予的角色与范围计算「将被授予的授权」，用于防提权比较。"""
    role_ids = sorted({binding.role_id for binding in bindings})
    permissions_map = repo.role_permission_map(db, tenant_id, role_ids)
    grants: list[ScopeGrant] = []
    for binding in bindings:
        permissions = frozenset(permissions_map.get(binding.role_id) or ())
        if not permissions:
            continue
        for scope in binding.scopes:
            grants.append(
                ScopeGrant(
                    role_binding_id=0,
                    role_id=binding.role_id,
                    scope_type=scope.scope_type,
                    scope_ids=frozenset(scope.scope_ids),
                    permissions=permissions,
                )
            )
    return EffectivePermissions(grants=tuple(grants), role_ids=frozenset(role_ids))


def _delete_bindings(db: Session, *, tenant_id: int, member_role_ids: list[int]) -> None:
    """删除若干成员角色绑定及其数据范围（调用方负责事务）。"""
    from app.modules.access.models_tenant import RoleBindingScope

    if not member_role_ids:
        return
    for scope in db.execute(
        select(RoleBindingScope).where(
            RoleBindingScope.tenant_id == tenant_id,
            RoleBindingScope.member_role_id.in_(member_role_ids),
        )
    ).scalars():
        db.delete(scope)
    db.flush()
    for member_role in db.execute(
        select(MemberRole).where(
            MemberRole.tenant_id == tenant_id,
            MemberRole.id.in_(member_role_ids),
        )
    ).scalars():
        db.delete(member_role)
    db.flush()


def _insert_binding(
    db: Session,
    *,
    tenant_id: int,
    member_id: int,
    binding: RoleBindingChoice,
    assigned_by: int | None,
) -> int:
    """写入一条角色绑定及其数据范围，返回成员角色绑定 ID（调用方负责事务）。"""
    from app.modules.access.models_tenant import RoleBindingScope

    member_role = MemberRole(
        tenant_id=tenant_id,
        role_id=binding.role_id,
        member_id=member_id,
        assigned_by=assigned_by,
    )
    db.add(member_role)
    db.flush()
    for scope in binding.scopes:
        if scope.scope_type == "all":
            db.add(
                RoleBindingScope(
                    tenant_id=tenant_id,
                    member_role_id=int(member_role.id),
                    scope_type="all",
                    scope_id=0,
                )
            )
            continue
        for scope_id in scope.scope_ids:
            db.add(
                RoleBindingScope(
                    tenant_id=tenant_id,
                    member_role_id=int(member_role.id),
                    scope_type=scope.scope_type,
                    scope_id=int(scope_id),
                )
            )
    db.flush()
    return int(member_role.id)


def _write_bindings(
    db: Session,
    *,
    tenant_id: int,
    member_id: int,
    bindings: list[RoleBindingChoice],
    assigned_by: int | None,
) -> None:
    """覆盖写入成员角色绑定与数据范围（调用方负责事务与权限校验）。"""
    existing = [
        int(row[0])
        for row in db.execute(
            select(MemberRole.id).where(
                MemberRole.tenant_id == tenant_id,
                MemberRole.member_id == member_id,
            )
        ).all()
    ]
    _delete_bindings(db, tenant_id=tenant_id, member_role_ids=existing)
    for binding in bindings:
        _insert_binding(
            db,
            tenant_id=tenant_id,
            member_id=member_id,
            binding=binding,
            assigned_by=assigned_by,
        )


def member_bindings_payload(db: Session, tenant_id: int, member_id: int) -> dict[str, Any]:
    """成员当前角色与数据范围（成员列表、bootstrap、角色页面共用）。"""
    rows = repo.list_member_role_rows(db, tenant_id, [member_id])
    binding_ids = [member_role_id for _, member_role_id, _ in rows]
    scopes_by_binding = repo.list_role_binding_scopes(db, tenant_id, binding_ids)
    roles = {int(role.id): role for role in repo.list_roles(db, tenant_id, include_disabled=True)}
    permissions_map = repo.role_permission_map(db, tenant_id, sorted(roles))
    role_codes: list[str] = []
    bindings: list[dict[str, Any]] = []
    for _, member_role_id, role_id in rows:
        role = roles.get(role_id)
        if role is None:
            continue
        if role.code not in role_codes:
            role_codes.append(role.code)
        scope_rows = scopes_by_binding.get(member_role_id, [])
        bindings.append(
            {
                "role_binding_id": str(member_role_id),
                "role_id": str(role_id),
                "role_code": role.code,
                "role_name": role.name,
                "permissions": permissions_map.get(role_id, []),
                "scopes": _scope_payload(scope_rows),
            }
        )
    return {"roles": role_codes, "bindings": bindings}


def _scope_payload(scope_rows: list[tuple[str, int]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[int]] = {}
    for scope_type, scope_id in scope_rows:
        if scope_type == "all":
            grouped.setdefault("all", [])
            continue
        if scope_id > 0:
            grouped.setdefault(scope_type, []).append(int(scope_id))
    result: list[dict[str, Any]] = []
    for scope_type in ("all", "stores", "stock_places"):
        if scope_type not in grouped:
            continue
        result.append(
            {
                "scope_type": scope_type,
                "scope_ids": [str(item) for item in sorted(grouped[scope_type])],
            }
        )
    return result


# --------------------------------------------------------------------------- 成员管理
def list_members(
    center: Session,
    db: Session,
    *,
    tenant_id: int,
    page: int,
    page_size: int,
    keyword: str = "",
    status: str | None = None,
) -> dict[str, Any]:
    """成员分页列表（含角色与数据范围摘要）。"""
    rows, total = repo.list_tenant_memberships(
        center,
        tenant_id,
        status=status,
        keyword=keyword,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    items: list[dict[str, Any]] = []
    for membership, user in rows:
        payload = member_bindings_payload(db, tenant_id, int(membership.id))
        items.append(
            {
                "id": str(membership.id),
                "user_id": str(user.id),
                "email": user.email,
                "display_name": user.display_name,
                "status": membership.status,
                "is_owner": bool(membership.is_owner),
                "joined_at": iso_utc(membership.joined_at),
                "created_at": iso_utc(membership.created_at),
                "roles": payload["roles"],
                "bindings": payload["bindings"],
            }
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def owner_removal_blocked(is_owner: bool, active_owner_count: int) -> bool:
    """停用成员 / 移除所有者角色是否会让租户没有有效所有者（纯函数）。"""
    return bool(is_owner) and int(active_owner_count) <= 1


def _owner_role(db: Session, tenant_id: int) -> Role | None:
    """预置所有者角色（编码固定，由开通流程写入）。"""
    from app.core.permissions import PredefinedRoleCode

    role = repo.get_role_by_code(db, tenant_id, PredefinedRoleCode.OWNER.value)
    if role is not None:
        return role
    for item in repo.list_roles(db, tenant_id, include_disabled=True):
        if item.code == PredefinedRoleCode.OWNER.value:
            return item
    return None


def update_member_status(
    center: Session,
    db: Session,
    *,
    tenant_id: int,
    member_id: int,
    status: str,
    actor_id: int,
) -> dict[str, Any]:
    """启用 / 停用成员；停用立即撤销会话，权限版本递增使缓存失效。

    最后有效所有者不可停用（``409 CONFLICT``），需先转移所有者。
    """
    if status not in {MembershipStatus.ACTIVE.value, MembershipStatus.DISABLED.value}:
        raise AppError(ErrorCode.VALIDATION_ERROR, "成员状态只能是 active 或 disabled")
    membership = repo.get_membership_by_id(center, tenant_id, member_id)
    if membership is None:
        raise AppError(ErrorCode.NOT_FOUND, "成员不存在")
    if membership.status == status:
        return {"id": str(membership.id), "status": membership.status}
    if status == MembershipStatus.DISABLED.value and owner_removal_blocked(
        bool(membership.is_owner), repo.count_active_owners(center, tenant_id)
    ):
        raise AppError(ErrorCode.CONFLICT, "不能停用最后一个有效所有者，请先转移所有者")
    with run_in_transaction(center):
        membership.status = status
        center.add(membership)
    if status == MembershipStatus.DISABLED.value:
        revoke_user_sessions(int(membership.user_id))
    with run_in_transaction(db):
        bump_permission_version(db, tenant_id)
        record_audit(
            db,
            tenant_id=tenant_id,
            action="member.status.update",
            resource_type="member",
            resource_id=int(membership.id),
            actor_id=actor_id,
            summary=f"成员状态变更为 {status}",
            payload={"status": status, "user_id": str(membership.user_id)},
        )
    return {"id": str(membership.id), "status": membership.status}


def replace_member_bindings(
    center: Session,
    db: Session,
    *,
    tenant_id: int,
    member_id: int,
    bindings: list[RoleBindingChoice],
    actor_id: int,
    actor_effective: EffectivePermissions,
) -> dict[str, Any]:
    """整体覆盖成员角色绑定：防提权校验 + 递增权限版本 + 审计。

    所有者成员必须保留所有者角色，移除会让租户没有有效所有者时返回 ``409 CONFLICT``。
    """
    membership = repo.get_membership_by_id(center, tenant_id, member_id)
    if membership is None:
        raise AppError(ErrorCode.NOT_FOUND, "成员不存在")
    validated = _validate_bindings(db, tenant_id, bindings)
    requested = _effective_for_bindings(db, tenant_id, validated)
    assert_can_grant(actor_effective, requested)
    if membership.is_owner:
        owner_role = _owner_role(db, tenant_id)
        keeps_owner = owner_role is not None and any(
            binding.role_id == int(owner_role.id) for binding in validated
        )
        if not keeps_owner:
            raise AppError(ErrorCode.CONFLICT, "所有者角色不能移除，请先转移所有者")
    with run_in_transaction(db):
        _write_bindings(
            db,
            tenant_id=tenant_id,
            member_id=member_id,
            bindings=validated,
            assigned_by=actor_id,
        )
        version = bump_permission_version(db, tenant_id)
        record_audit(
            db,
            tenant_id=tenant_id,
            action="member.roles.replace",
            resource_type="member",
            resource_id=member_id,
            actor_id=actor_id,
            summary="覆盖成员角色与数据范围",
            payload={
                "bindings": _bindings_to_json(validated),
                "permission_version": version,
            },
        )
    return {"id": str(member_id), **member_bindings_payload(db, tenant_id, member_id)}


# --------------------------------------------------------------------------- 所有者转移
def transfer_owner(
    center: Session,
    db: Session,
    *,
    tenant_id: int,
    target_member_id: int,
    actor_user_id: int,
    actor_member_id: int | None = None,
    actor_is_platform_admin: bool = False,
) -> dict[str, Any]:
    """转移租户所有者：仅当前有效所有者或平台管理员可调用。

    * 目标必须是当前租户的 ``active`` 成员，且不能已经是所有者；
    * 租户库：新所有者获得 ``owner`` 角色（全部数据范围），旧所有者移除 ``owner`` 角色
      （保留其他角色），权限版本递增使缓存失效，写审计；
    * 中心库：旧成员 ``is_owner=False``、新成员 ``is_owner=True``、``tenants.owner_user_id`` 更新；
    * 两个库无法跨库事务，先写租户库（权限）再写中心库（归属）；任一步失败可用同一接口重试。
    """
    from app.core.permissions import PredefinedRoleCode

    tenant = repo.get_tenant(center, tenant_id)
    if tenant is None:
        raise AppError(ErrorCode.TENANT_NOT_FOUND)
    target = repo.get_membership_by_id(center, tenant_id, target_member_id)
    if target is None:
        raise AppError(ErrorCode.NOT_FOUND, "目标成员不存在")
    if target.status != MembershipStatus.ACTIVE.value:
        raise AppError(ErrorCode.CONFLICT, "目标成员必须是有效状态，请先激活成员")
    if target.is_owner:
        raise AppError(ErrorCode.CONFLICT, "目标成员已经是所有者")
    owners = [item for item in repo.list_owner_memberships(center, tenant_id) if item.status == MembershipStatus.ACTIVE.value]
    if not owners:
        raise AppError(ErrorCode.CONFLICT, "租户没有有效所有者，无法转移")
    if not actor_is_platform_admin:
        actor_membership = next(
            (item for item in owners if actor_member_id is not None and int(item.id) == int(actor_member_id)),
            None,
        )
        if actor_membership is None:
            raise AppError(ErrorCode.PERMISSION_DENIED, "只有当前所有者可以转移所有者")

    owner_role = _owner_role(db, tenant_id)
    if owner_role is None:
        raise AppError(ErrorCode.CONFLICT, "租户缺少预置所有者角色，无法转移")
    owner_role_id = int(owner_role.id)
    previous_owner_ids: list[str] = []

    # 1) 租户库：直写角色绑定与数据范围、递增权限版本、写审计
    with run_in_transaction(db):
        target_rows = repo.list_member_role_rows(db, tenant_id, [target_member_id])
        target_owner_bindings = [
            member_role_id for _, member_role_id, role_id in target_rows if role_id == owner_role_id
        ]
        _delete_bindings(db, tenant_id=tenant_id, member_role_ids=target_owner_bindings)
        _insert_binding(
            db,
            tenant_id=tenant_id,
            member_id=target_member_id,
            binding=RoleBindingChoice(
                role_id=owner_role_id,
                scopes=(ScopeChoice(scope_type="all", scope_ids=()),),
            ),
            assigned_by=actor_user_id,
        )
        for owner in owners:
            rows = repo.list_member_role_rows(db, tenant_id, [int(owner.id)])
            owner_bindings = [
                member_role_id for _, member_role_id, role_id in rows if role_id == owner_role_id
            ]
            if owner_bindings:
                _delete_bindings(db, tenant_id=tenant_id, member_role_ids=owner_bindings)
            previous_owner_ids.append(str(owner.id))
        permission_version = bump_permission_version(db, tenant_id)
        record_audit(
            db,
            tenant_id=tenant_id,
            action="member.owner.transfer",
            resource_type="member",
            resource_id=target_member_id,
            actor_id=actor_user_id,
            summary=f"转移租户所有者给成员 {target_member_id}",
            payload={
                "from_member_ids": previous_owner_ids,
                "to_member_id": str(target_member_id),
                "to_user_id": str(target.user_id),
                "permission_version": permission_version,
                "operator": "platform_admin" if actor_is_platform_admin else "owner",
                "role_code": PredefinedRoleCode.OWNER.value,
            },
        )

    # 2) 中心库：所有者标记与负责人归属
    with run_in_transaction(center):
        for owner in owners:
            owner.is_owner = False
            center.add(owner)
        target.is_owner = True
        center.add(target)
        tenant.owner_user_id = int(target.user_id)
        center.add(tenant)

    return {
        "tenant_id": str(tenant_id),
        "owner_member_id": str(target_member_id),
        "owner_user_id": str(target.user_id),
        "previous_owner_member_ids": previous_owner_ids,
        "permission_version": permission_version,
    }


# --------------------------------------------------------------------------- 角色管理
def list_roles(db: Session, tenant_id: int) -> list[dict[str, Any]]:
    roles = repo.list_roles(db, tenant_id, include_disabled=True)
    permissions_map = repo.role_permission_map(db, tenant_id, [int(role.id) for role in roles])
    return [
        {
            "id": str(role.id),
            "code": role.code,
            "name": role.name,
            "description": role.description,
            "is_system": bool(role.is_system),
            "copied_from_role_id": str(role.copied_from_role_id) if role.copied_from_role_id else None,
            "status": role.status,
            "permissions": permissions_map.get(int(role.id), []),
        }
        for role in roles
    ]


def _validate_permission_codes(permissions: list[str]) -> list[str]:
    normalized = sorted({str(item).strip() for item in permissions if str(item).strip()})
    unknown = [item for item in normalized if item not in ALL_PERMISSIONS]
    if unknown:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "存在未知权限编码",
            details={"permissions": unknown},
        )
    return normalized


def create_role(
    db: Session,
    *,
    tenant_id: int,
    code: str,
    name: str,
    description: str = "",
    permissions: list[str],
    copy_from_role_id: int | None,
    actor_id: int,
    actor_effective: EffectivePermissions,
) -> dict[str, Any]:
    """创建角色；可复制现有角色权限；校验编码唯一与防提权。"""
    normalized_code = (code or "").strip()
    if not normalized_code or len(normalized_code) > 64:
        raise AppError(ErrorCode.VALIDATION_ERROR, "角色编码不能为空且不超过 64 个字符")
    if repo.get_role_by_code(db, tenant_id, normalized_code) is not None:
        raise AppError(ErrorCode.CONFLICT, "角色编码已存在")
    codes = _validate_permission_codes(permissions)
    if copy_from_role_id is not None:
        source = repo.get_role(db, tenant_id, copy_from_role_id)
        if source is None:
            raise AppError(ErrorCode.NOT_FOUND, "复制来源角色不存在")
        copied_codes = repo.role_permission_map(db, tenant_id, [copy_from_role_id]).get(copy_from_role_id, [])
        codes = _validate_permission_codes(codes + copied_codes) if codes else copied_codes
    assert_can_manage_permissions(actor_effective, set(codes))
    with run_in_transaction(db):
        role = Role(
            tenant_id=tenant_id,
            code=normalized_code,
            name=(name or normalized_code)[:64],
            description=description or "",
            is_system=False,
            copied_from_role_id=copy_from_role_id,
            status="active",
        )
        db.add(role)
        db.flush()
        for permission_code in codes:
            db.add(
                RolePermission(
                    tenant_id=tenant_id,
                    role_id=int(role.id),
                    permission_code=permission_code,
                )
            )
        bump_permission_version(db, tenant_id)
        record_audit(
            db,
            tenant_id=tenant_id,
            action="role.create",
            resource_type="role",
            resource_id=int(role.id),
            actor_id=actor_id,
            summary=f"创建角色：{role.name}",
            payload={"code": role.code, "permissions": codes},
        )
        return {
            "id": str(role.id),
            "code": role.code,
            "name": role.name,
            "description": role.description,
            "is_system": bool(role.is_system),
            "copied_from_role_id": str(role.copied_from_role_id) if role.copied_from_role_id else None,
            "status": role.status,
            "permissions": codes,
        }


def update_role(
    db: Session,
    *,
    tenant_id: int,
    role_id: int,
    name: str | None = None,
    description: str | None = None,
    permissions: list[str] | None = None,
    actor_id: int,
    actor_effective: EffectivePermissions,
) -> dict[str, Any]:
    """修改角色名称 / 说明 / 权限；权限变更后递增版本。"""
    role = repo.get_role(db, tenant_id, role_id)
    if role is None:
        raise AppError(ErrorCode.NOT_FOUND, "角色不存在")
    codes: list[str] | None = None
    if permissions is not None:
        codes = _validate_permission_codes(permissions)
        current = repo.role_permission_map(db, tenant_id, [role_id]).get(role_id, [])
        assert_can_manage_permissions(actor_effective, set(codes) - set(current))
    with run_in_transaction(db):
        if name is not None:
            if not name.strip():
                raise AppError(ErrorCode.VALIDATION_ERROR, "角色名称不能为空")
            role.name = name.strip()[:64]
        if description is not None:
            role.description = description[:255]
        db.add(role)
        if codes is not None:
            for row in db.query(RolePermission).filter(
                RolePermission.tenant_id == tenant_id,
                RolePermission.role_id == role_id,
            ):
                db.delete(row)
            db.flush()
            for permission_code in codes:
                db.add(
                    RolePermission(
                        tenant_id=tenant_id,
                        role_id=role_id,
                        permission_code=permission_code,
                    )
                )
            bump_permission_version(db, tenant_id)
        record_audit(
            db,
            tenant_id=tenant_id,
            action="role.update",
            resource_type="role",
            resource_id=role_id,
            actor_id=actor_id,
            summary=f"修改角色：{role.name}",
            payload={"permissions": codes if codes is not None else "unchanged"},
        )
    return {
        "id": str(role.id),
        "code": role.code,
        "name": role.name,
        "description": role.description,
        "is_system": bool(role.is_system),
        "copied_from_role_id": str(role.copied_from_role_id) if role.copied_from_role_id else None,
        "status": role.status,
        "permissions": repo.role_permission_map(db, tenant_id, [role_id]).get(role_id, []),
    }


def delete_role(
    db: Session,
    *,
    tenant_id: int,
    role_id: int,
    actor_id: int,
) -> None:
    """删除角色：系统预置角色或仍被绑定的角色返回 409。"""
    role = repo.get_role(db, tenant_id, role_id)
    if role is None:
        raise AppError(ErrorCode.NOT_FOUND, "角色不存在")
    if role.is_system:
        raise AppError(ErrorCode.CONFLICT, "系统预置角色不可删除，请复制后修改")
    if repo.count_role_bindings(db, tenant_id, role_id) > 0:
        raise AppError(ErrorCode.CONFLICT, "角色仍被成员使用，无法删除")
    with run_in_transaction(db):
        for row in db.query(RolePermission).filter(
            RolePermission.tenant_id == tenant_id,
            RolePermission.role_id == role_id,
        ):
            db.delete(row)
        db.delete(role)
        db.flush()
        bump_permission_version(db, tenant_id)
        record_audit(
            db,
            tenant_id=tenant_id,
            action="role.delete",
            resource_type="role",
            resource_id=role_id,
            actor_id=actor_id,
            summary=f"删除角色：{role.name}",
            payload={"code": role.code},
        )


# --------------------------------------------------------------------------- 预置角色
def seed_predefined_roles(
    db: Session, tenant_id: int, *, created_by: int | None = None
) -> dict[str, Role]:
    """写入预置角色与权限（幂等，用于开通流程与测试）。"""
    existing = {role.code: role for role in repo.list_roles(db, tenant_id, include_disabled=True)}
    for code, permission_codes in PREDEFINED_ROLE_PERMISSIONS.items():
        role = existing.get(code)
        if role is None:
            role = Role(
                tenant_id=tenant_id,
                code=code,
                name=_ROLE_NAMES.get(code, code),
                description=_ROLE_DESCRIPTIONS.get(code, ""),
                is_system=True,
                copied_from_role_id=None,
                status="active",
            )
            db.add(role)
            db.flush()
            existing[code] = role
        current = set(repo.role_permission_map(db, tenant_id, [int(role.id)]).get(int(role.id), []))
        missing = sorted(set(permission_codes) - current)
        for permission_code in missing:
            db.add(
                RolePermission(
                    tenant_id=tenant_id,
                    role_id=int(role.id),
                    permission_code=permission_code,
                )
            )
        db.flush()
    return existing


_ROLE_NAMES: dict[str, str] = {
    "owner": "所有者",
    "admin": "管理员",
    "catalog_operator": "商品运营",
    "order_operator": "订单运营",
    "stock_operator": "库存操作员",
    "readonly": "只读",
}

_ROLE_DESCRIPTIONS: dict[str, str] = {
    "owner": "租户负责人，拥有全部权限，不可删除与停用",
    "admin": "租户管理员，拥有全部权限",
    "catalog_operator": "维护商品、SKU 与平台商品映射",
    "order_operator": "同步订单并推送库存",
    "stock_operator": "调整库存与推送库存",
    "readonly": "只读查看租户数据",
}


# --------------------------------------------------------------------------- 启动数据
def bootstrap_payload(
    db: Session,
    context: TenantContext,
    snapshot: AccessSnapshot,
) -> dict[str, Any]:
    """租户启动数据：当前租户、成员角色与绑定、有效权限、功能开关、权限版本。"""
    member = member_bindings_payload(db, context.tenant_id, context.member_id)
    return {
        "tenant": {
            "id": str(context.tenant_id),
            "code": context.tenant_code,
            "name": context.tenant_name,
            "status": context.tenant_status,
            "timezone": context.timezone,
        },
        "member": {
            "id": str(context.member_id),
            "roles": member["roles"],
            "bindings": member["bindings"],
        },
        "permissions": sorted(snapshot.effective.permissions),
        "feature_flags": feature_flag_map(snapshot.feature_flags),
        "permission_version": snapshot.permission_version,
        "server_time": iso_utc(utc_now()),
    }


# --------------------------------------------------------------------------- 平台辅助
def tenant_detail_payload(center: Session, tenant: Tenant) -> dict[str, Any]:
    """平台租户详情：基础信息 + 数据库状态 + 开通步骤 + 负责人 + 负责人邀请摘要。"""
    database = repo.get_tenant_database(center, int(tenant.id))
    owner_email = ""
    if tenant.owner_user_id:
        owner = repo.get_user(center, int(tenant.owner_user_id))
        owner_email = owner.email if owner else ""
    jobs = repo.list_provisioning_jobs(center, int(tenant.id))
    owner_invitation = latest_owner_invitation(center, tenant)
    return {
        "id": str(tenant.id),
        "code": tenant.code,
        "name": tenant.name,
        "status": tenant.status,
        "timezone": tenant.timezone,
        "plan_id": str(tenant.plan_id) if tenant.plan_id else None,
        "owner_user_id": str(tenant.owner_user_id) if tenant.owner_user_id else None,
        "owner_email": owner_email,
        "suspended_at": iso_utc(tenant.suspended_at),
        "suspended_reason": tenant.suspended_reason,
        "created_at": iso_utc(tenant.created_at),
        # 负责人邀请摘要：不含 token（明文令牌只在重新签发接口返回一次）
        "owner_invitation": invitation_payload(owner_invitation) if owner_invitation else None,
        "database": (
            {
                "host": database.host,
                "port": database.port,
                "database_name": database.database_name,
                "username": database.username,
                "credentials_version": database.credentials_version,
                "schema_version": database.schema_version,
                "status": database.status,
                "last_migrated_at": iso_utc(database.last_migrated_at),
            }
            if database is not None
            else None
        ),
        "provisioning": [
            {
                "id": str(job.id),
                "step": job.step,
                "status": job.status,
                "attempts": job.attempts,
                "error_message": job.error_message,
                "started_at": iso_utc(job.started_at),
                "finished_at": iso_utc(job.finished_at),
            }
            for job in jobs
        ],
    }


def tenant_list_payload(center: Session, tenant: Tenant) -> dict[str, Any]:
    """平台租户列表行。"""
    database = repo.get_tenant_database(center, int(tenant.id))
    jobs = repo.list_provisioning_jobs(center, int(tenant.id))
    failed = next((job for job in jobs if job.status == "failed"), None)
    return {
        "id": str(tenant.id),
        "code": tenant.code,
        "name": tenant.name,
        "status": tenant.status,
        "timezone": tenant.timezone,
        "plan_id": str(tenant.plan_id) if tenant.plan_id else None,
        "database_status": database.status if database is not None else None,
        "provisioning_status": jobs[-1].status if jobs else None,
        "provisioning_error": failed.error_message if failed is not None else None,
        "created_at": iso_utc(tenant.created_at),
    }


def permission_codes_catalog() -> dict[str, Any]:
    """权限编码目录响应（分组 + 中文名）。"""
    from app.modules.identity.permission_catalog import catalogue

    return {"groups": catalogue(), "permissions": sorted(ALL_PERMISSIONS)}


__all__ = [
    "DEFAULT_STOCK_PLACE_CODE",
    "Permission",
    "RoleBindingChoice",
    "ScopeChoice",
    "accept_invitation",
    "authenticate",
    "bootstrap_payload",
    "change_password",
    "create_member_invitation",
    "create_role",
    "delete_role",
    "is_platform_admin",
    "iso_utc",
    "list_members",
    "list_roles",
    "list_user_tenants",
    "load_authenticated_user",
    "load_tenant_access",
    "load_tenant_db_route",
    "member_bindings_payload",
    "normalize_email",
    "permission_codes_catalog",
    "preview_invitation",
    "record_login",
    "replace_member_bindings",
    "run_in_transaction",
    "seed_predefined_roles",
    "tenant_detail_payload",
    "tenant_list_payload",
    "update_member_status",
    "update_role",
    "user_payload",
]
