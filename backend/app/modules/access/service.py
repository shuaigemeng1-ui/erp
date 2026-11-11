"""权限快照服务：有效角色绑定、数据范围、功能开关与防提权校验。

契约（``app/api/deps.py`` 依赖，签名固定，见 docs/architecture.md）：

* ``AccessSnapshot``：``permission_version`` + ``effective`` + ``feature_flags``；
* ``load_access_snapshot(db, tenant_id, member_id)``：租户库 Session，返回当前有效授权；
* ``bump_permission_version(db, tenant_id)``：权限 / 绑定 / 成员变更后递增，返回新版本。

规则（见 plan.txt §3「多角色计算规则」）：

1. 权限与数据范围不可拆分：每个 ``ScopeGrant`` 来自同一条成员角色绑定；
2. 空范围代表没有授权（绑定没有任何范围行时不产生授权）；
3. 缓存键包含租户、成员与权限版本，版本递增后旧缓存自然失效；
4. 角色分配 / 编辑之前必须校验「授予的权限不超出操作者」，防止自行提权。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.access import EffectivePermissions, ScopeGrant
from app.core.errors import AppError, ErrorCode
from app.core.permissions import SCOPE_ALL, SCOPE_STOCK_PLACES, SCOPE_STORES, SCOPE_TYPES
from app.core.redis import cache_get_json, cache_set_json
from app.db.base import utcnow
from app.modules.access.models_tenant import (
    FeatureSetting,
    RoleBindingScope,
    TenantSettings,
)
from app.modules.identity.models_tenant import MemberRole, Role, RolePermission

logger = logging.getLogger(__name__)

# 权限快照缓存时长（秒）；版本号变化后键不同，无需主动删除
ACCESS_CACHE_TTL_SECONDS = 300

FEATURE_ORDER_SYNC = "order_sync"
FEATURE_STOCK_PUSH = "stock_push"
DEFAULT_FEATURE_CODES: tuple[str, ...] = (FEATURE_ORDER_SYNC, FEATURE_STOCK_PUSH)


@dataclass(frozen=True, slots=True)
class AccessSnapshot:
    """一次请求 / 任务内的权限快照（不可变）。"""

    permission_version: int
    effective: EffectivePermissions
    feature_flags: frozenset[str] = field(default_factory=frozenset)
    loaded_at: datetime | None = None


# --------------------------------------------------------------------------- 纯函数
def access_cache_key(tenant_id: int, member_id: int, permission_version: int) -> str:
    """缓存键：必须同时包含租户、成员与权限版本。"""
    return f"tenant:{tenant_id}:member:{member_id}:access:v{permission_version}"


def build_grants(
    binding_rows: list[tuple[int, int]],
    permissions_by_role: dict[int, set[str] | frozenset[str]],
    scopes_by_binding: dict[int, list[tuple[str, int]]],
) -> tuple[ScopeGrant, ...]:
    """把仓储查询结果转换为 ``ScopeGrant`` 元组（纯函数，可单测）。

    ``binding_rows``：``[(member_role_id, role_id)]``；
    ``permissions_by_role``：``{role_id: {permission_code}}``；
    ``scopes_by_binding``：``{member_role_id: [(scope_type, scope_id)]}``。
    """
    grants: list[ScopeGrant] = []
    for binding_id, role_id in binding_rows:
        permissions = frozenset(permissions_by_role.get(role_id) or ())
        if not permissions:
            # 角色没有任何权限时，绑定不产生授权
            continue
        scope_rows = scopes_by_binding.get(binding_id) or []
        if not scope_rows:
            # 空范围代表没有授权，必须显式指定 all / stores / stock_places
            continue
        has_all = False
        grouped: dict[str, set[int]] = {}
        for scope_type, scope_id in scope_rows:
            if scope_type == SCOPE_ALL:
                has_all = True
                continue
            if scope_type not in {SCOPE_STORES, SCOPE_STOCK_PLACES}:
                logger.warning("忽略未知数据范围类型: %s", scope_type)
                continue
            if scope_id and scope_id > 0:
                grouped.setdefault(scope_type, set()).add(int(scope_id))
        if has_all:
            grants.append(
                ScopeGrant(
                    role_binding_id=binding_id,
                    role_id=role_id,
                    scope_type=SCOPE_ALL,
                    scope_ids=frozenset(),
                    permissions=permissions,
                )
            )
            continue
        for scope_type, scope_ids in grouped.items():
            grants.append(
                ScopeGrant(
                    role_binding_id=binding_id,
                    role_id=role_id,
                    scope_type=scope_type,
                    scope_ids=frozenset(scope_ids),
                    permissions=permissions,
                )
            )
    return tuple(grants)


def snapshot_to_cache(snapshot: AccessSnapshot) -> dict[str, object]:
    """快照 -> 可 JSON 序列化的缓存结构。"""
    return {
        "permission_version": snapshot.permission_version,
        "feature_flags": sorted(snapshot.feature_flags),
        "grants": [
            {
                "role_binding_id": grant.role_binding_id,
                "role_id": grant.role_id,
                "scope_type": grant.scope_type,
                "scope_ids": sorted(grant.scope_ids),
                "permissions": sorted(grant.permissions),
            }
            for grant in snapshot.effective.grants
        ],
    }


def snapshot_from_cache(payload: dict[str, object], loaded_at: datetime | None = None) -> AccessSnapshot:
    """缓存结构 -> 快照（脏数据容错，异常返回空授权）。"""
    grants: list[ScopeGrant] = []
    raw_grants = payload.get("grants") or []
    if isinstance(raw_grants, list):
        for item in raw_grants:
            if not isinstance(item, dict):
                continue
            try:
                grants.append(
                    ScopeGrant(
                        role_binding_id=int(item["role_binding_id"]),  # type: ignore[arg-type]
                        role_id=int(item["role_id"]),  # type: ignore[arg-type]
                        scope_type=str(item["scope_type"]),
                        scope_ids=frozenset(int(value) for value in (item.get("scope_ids") or [])),
                        permissions=frozenset(str(value) for value in (item.get("permissions") or [])),
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("权限快照缓存数据损坏，已忽略: %s", item)
    flags = payload.get("feature_flags") or []
    return AccessSnapshot(
        permission_version=int(payload.get("permission_version") or 1),
        effective=EffectivePermissions(grants=tuple(grants), role_ids=frozenset(g.role_id for g in grants)),
        feature_flags=frozenset(str(item) for item in flags) if isinstance(flags, list) else frozenset(),
        loaded_at=loaded_at,
    )


def feature_flag_map(flags: frozenset[str] | set[str]) -> dict[str, bool]:
    """功能开关响应结构：已知功能显式给出布尔值，避免前端缺键。"""
    known = set(DEFAULT_FEATURE_CODES) | set(flags)
    return {code: code in flags for code in sorted(known)}


def validate_scope_choice(scope_type: str, scope_ids: list[int]) -> tuple[str, list[int]]:
    """校验单个数据范围选择（纯函数）：空范围无授权，全部范围不得携带 ID。"""
    if scope_type not in SCOPE_TYPES:
        raise AppError(ErrorCode.VALIDATION_ERROR, f"未知数据范围类型: {scope_type}")
    normalized = sorted({int(item) for item in scope_ids if int(item) > 0})
    if scope_type == SCOPE_ALL:
        if normalized:
            raise AppError(ErrorCode.VALIDATION_ERROR, "全部数据范围不允许指定对象ID")
        return scope_type, []
    if not normalized:
        raise AppError(ErrorCode.VALIDATION_ERROR, "指定数据范围必须至少包含一个对象ID")
    return scope_type, normalized


def scope_covered_by(
    actor: EffectivePermissions, permission: str, scope_type: str, scope_ids: frozenset[int]
) -> bool:
    """操作者的授权是否覆盖待授予的数据范围。"""
    if not actor.has(permission):
        return False
    if scope_type == SCOPE_ALL:
        # 「全部」范围必须显式拥有，不能由其他维度的局部范围组合而来
        return SCOPE_ALL in actor.granted_scope_types(permission)
    allowed = actor.allowed_ids(permission, scope_type)
    if allowed is None:
        return True
    return scope_ids <= allowed


def assert_can_grant(actor: EffectivePermissions | None, requested: EffectivePermissions) -> None:
    """防提权：不得授予操作者自身没有的权限，也不得超出其数据范围。

    ``actor`` 为空表示系统任务（开通流程 / 初始化命令），不做限制。
    """
    if actor is None:
        return
    for grant in requested.grants:
        for permission in sorted(grant.permissions):
            if not actor.has(permission):
                raise AppError(
                    ErrorCode.ROLE_ASSIGN_FORBIDDEN,
                    f"无权授予权限：{permission}",
                    details={"permission": permission},
                )
            if not scope_covered_by(actor, permission, grant.scope_type, grant.scope_ids):
                raise AppError(
                    ErrorCode.ROLE_ASSIGN_FORBIDDEN,
                    f"无权授予数据范围：{permission} / {grant.scope_type}",
                    details={
                        "permission": permission,
                        "scope_type": grant.scope_type,
                        "scope_ids": sorted(grant.scope_ids),
                    },
                )


def assert_can_manage_permissions(actor: EffectivePermissions | None, permissions: set[str]) -> None:
    """编辑角色权限时的防提权：新增的每个权限都必须是操作者拥有的。"""
    if actor is None:
        return
    missing = sorted(code for code in permissions if not actor.has(code))
    if missing:
        raise AppError(
            ErrorCode.ROLE_ASSIGN_FORBIDDEN,
            "无权授予自己不具备的权限",
            details={"permissions": missing},
        )


# --------------------------------------------------------------------------- 包装载
def _load_permission_version(db: Session, tenant_id: int) -> tuple[int, TenantSettings | None]:
    settings_row = db.execute(
        select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if settings_row is None:
        return 1, None
    return int(settings_row.permission_version or 1), settings_row


def _load_grants(db: Session, tenant_id: int, member_id: int) -> tuple[ScopeGrant, ...]:
    binding_rows = [
        (int(row[0]), int(row[1]))
        for row in db.execute(
            select(MemberRole.id, MemberRole.role_id)
            .join(
                Role,
                (Role.id == MemberRole.role_id) & (Role.tenant_id == MemberRole.tenant_id),
            )
            .where(
                MemberRole.tenant_id == tenant_id,
                MemberRole.member_id == member_id,
                Role.status == "active",
            )
        ).all()
    ]
    if not binding_rows:
        return ()
    role_ids = sorted({role_id for _, role_id in binding_rows})
    binding_ids = [binding_id for binding_id, _ in binding_rows]

    permissions_by_role: dict[int, set[str]] = {}
    for role_id, permission_code in db.execute(
        select(RolePermission.role_id, RolePermission.permission_code).where(
            RolePermission.tenant_id == tenant_id,
            RolePermission.role_id.in_(role_ids),
        )
    ).all():
        permissions_by_role.setdefault(int(role_id), set()).add(str(permission_code))

    scopes_by_binding: dict[int, list[tuple[str, int]]] = {}
    for member_role_id, scope_type, scope_id in db.execute(
        select(
            RoleBindingScope.member_role_id,
            RoleBindingScope.scope_type,
            RoleBindingScope.scope_id,
        ).where(
            RoleBindingScope.tenant_id == tenant_id,
            RoleBindingScope.member_role_id.in_(binding_ids),
        )
    ).all():
        scopes_by_binding.setdefault(int(member_role_id), []).append((str(scope_type), int(scope_id)))

    return build_grants(binding_rows, permissions_by_role, scopes_by_binding)


def _load_feature_flags(db: Session, tenant_id: int, settings_row: TenantSettings | None) -> frozenset[str]:
    flags: set[str] = set()
    for feature_code in db.execute(
        select(FeatureSetting.feature_code).where(
            FeatureSetting.tenant_id == tenant_id,
            FeatureSetting.enabled.is_(True),
        )
    ).scalars():
        flags.add(str(feature_code))
    if settings_row is not None:
        if settings_row.order_sync_enabled:
            flags.add(FEATURE_ORDER_SYNC)
        if settings_row.stock_push_enabled:
            flags.add(FEATURE_STOCK_PUSH)
    return frozenset(flags)


def load_access_snapshot(db: Session, tenant_id: int, member_id: int) -> AccessSnapshot:
    """加载成员的有效授权快照（租户库 Session）。

    缓存键含租户、成员与权限版本；Redis 不可用时降级为直接查库，不影响业务请求。
    """
    version, settings_row = _load_permission_version(db, tenant_id)
    cache_key = access_cache_key(tenant_id, member_id, version)
    try:
        cached = cache_get_json(cache_key)
        if isinstance(cached, dict):
            return snapshot_from_cache(cached, loaded_at=utcnow())
    except Exception:  # noqa: BLE001 - 缓存故障不影响授权计算
        logger.warning("读取权限快照缓存失败 tenant=%s member=%s", tenant_id, member_id, exc_info=True)

    grants = _load_grants(db, tenant_id, member_id)
    snapshot = AccessSnapshot(
        permission_version=version,
        effective=EffectivePermissions(
            grants=grants,
            role_ids=frozenset(grant.role_id for grant in grants),
        ),
        feature_flags=_load_feature_flags(db, tenant_id, settings_row),
        loaded_at=utcnow(),
    )
    try:
        cache_set_json(cache_key, snapshot_to_cache(snapshot), ttl_seconds=ACCESS_CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001 - 缓存写入失败忽略
        logger.warning("写入权限快照缓存失败 tenant=%s member=%s", tenant_id, member_id, exc_info=True)
    return snapshot


def bump_permission_version(db: Session, tenant_id: int) -> int:
    """递增租户权限版本（成员 / 角色 / 数据范围变更后调用），返回新版本号。"""
    settings_row = db.execute(
        select(TenantSettings).where(TenantSettings.tenant_id == tenant_id).with_for_update()
    ).scalar_one_or_none()
    if settings_row is None:
        settings_row = TenantSettings(tenant_id=tenant_id, permission_version=1)
        db.add(settings_row)
        db.flush()
        return int(settings_row.permission_version)
    settings_row.permission_version = int(settings_row.permission_version or 1) + 1
    db.flush()
    return int(settings_row.permission_version)


def ensure_tenant_settings(db: Session, tenant_id: int, *, timezone: str = "Asia/Shanghai") -> TenantSettings:
    """确保租户设置存在（开通与测试使用），不存在时创建。"""
    settings_row = db.execute(
        select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if settings_row is not None:
        return settings_row
    settings_row = TenantSettings(
        tenant_id=tenant_id,
        permission_version=1,
        timezone=timezone,
        company_name="",
        order_sync_enabled=True,
        stock_push_enabled=False,
    )
    db.add(settings_row)
    db.flush()
    return settings_row
