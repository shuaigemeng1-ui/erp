"""权限与数据范围计算（纯函数，无数据库依赖）。

规则（见 plan.txt §3「多角色计算规则」）：

1. 一次操作被允许 = 存在一个有效角色绑定，既具有该操作权限，又覆盖目标数据范围；
   不同绑定的授权结果取并集，禁止把「某绑定的操作权限」与「另一绑定的数据范围」拼在一起。
2. 同一绑定的多个店铺 / 库存地点取并集。
3. 空范围代表没有授权；「全部」范围必须显式写在范围类型里。
4. 一次检查只针对一个维度：店铺或库存地点（首版不引入部门树）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.permissions import SCOPE_ALL, SCOPE_STOCK_PLACES, SCOPE_STORES


@dataclass(frozen=True, slots=True)
class ScopeGrant:
    """一条角色绑定的授权：权限集合 + 数据范围。"""

    role_binding_id: int
    role_id: int
    scope_type: str
    scope_ids: frozenset[int] = frozenset()
    permissions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.scope_type not in {SCOPE_ALL, SCOPE_STORES, SCOPE_STOCK_PLACES}:
            raise ValueError(f"未知数据范围类型: {self.scope_type}")
        if self.scope_type == SCOPE_ALL and self.scope_ids:
            raise ValueError("全部范围不允许携带具体 ID")
        if self.scope_type in {SCOPE_STORES, SCOPE_STOCK_PLACES} and not self.scope_ids:
            raise ValueError("指定范围必须至少包含一个对象 ID")


@dataclass(frozen=True, slots=True)
class EffectivePermissions:
    """一次租户请求内的完整有效授权集合。"""

    grants: tuple[ScopeGrant, ...] = field(default_factory=tuple)
    role_ids: frozenset[int] = frozenset()

    @property
    def permissions(self) -> frozenset[str]:
        result: set[str] = set()
        for grant in self.grants:
            result.update(grant.permissions)
        return frozenset(result)

    def has(self, permission: str) -> bool:
        """是否拥有该功能权限（不判断数据范围）。"""
        return any(permission in grant.permissions for grant in self.grants)

    def granted_scope_types(self, permission: str) -> frozenset[str]:
        return frozenset(
            grant.scope_type for grant in self.grants if permission in grant.permissions
        )

    def covers(
        self,
        permission: str,
        *,
        store_id: int | None = None,
        stock_place_id: int | None = None,
    ) -> bool:
        """是否存在一条绑定同时满足「有权限」且「覆盖目标数据」。

        ``store_id`` / ``stock_place_id`` 都为空时只判断功能权限。
        """
        if store_id is None and stock_place_id is None:
            return self.has(permission)

        for grant in self.grants:
            if permission not in grant.permissions:
                continue
            if grant.scope_type == SCOPE_ALL:
                return True
            if store_id is not None and grant.scope_type == SCOPE_STORES:
                if store_id in grant.scope_ids:
                    return True
            if stock_place_id is not None and grant.scope_type == SCOPE_STOCK_PLACES:
                if stock_place_id in grant.scope_ids:
                    return True
        return False

    def allowed_ids(self, permission: str, scope_type: str) -> frozenset[int] | None:
        """返回该权限在某维度上的可见 ID 集合；``None`` 表示全部。"""
        if not self.has(permission):
            return frozenset()
        for grant in self.grants:
            if permission in grant.permissions and grant.scope_type == SCOPE_ALL:
                return None
        ids: set[int] = set()
        for grant in self.grants:
            if permission in grant.permissions and grant.scope_type == scope_type:
                ids.update(grant.scope_ids)
        return frozenset(ids)

    def filter_ids(self, permission: str, scope_type: str, candidate_ids: list[int]) -> list[int]:
        allowed = self.allowed_ids(permission, scope_type)
        if allowed is None:
            return list(candidate_ids)
        return [item for item in candidate_ids if item in allowed]


def effective_from_context(context: object) -> "EffectivePermissions":
    """从 ``TenantContext`` 还原有效权限（Service / Worker 复用）。"""
    grants = tuple(getattr(context, "scope_grants", ()) or ())
    role_ids = frozenset(getattr(item, "role_id", 0) for item in grants)
    return EffectivePermissions(grants=grants, role_ids=role_ids)


def grants_from_rows(rows: list[dict[str, object]]) -> tuple[ScopeGrant, ...]:
    """把仓储层查询结果（字典）转换为 ``ScopeGrant`` 元组。"""
    grants: list[ScopeGrant] = []
    for row in rows:
        scope_type = str(row["scope_type"])
        raw_ids = row.get("scope_ids") or []
        scope_ids = frozenset(int(item) for item in raw_ids)  # type: ignore[arg-type]
        permissions = frozenset(str(item) for item in (row.get("permissions") or []))
        grants.append(
            ScopeGrant(
                role_binding_id=int(row["role_binding_id"]),  # type: ignore[arg-type]
                role_id=int(row["role_id"]),  # type: ignore[arg-type]
                scope_type=scope_type,
                scope_ids=scope_ids,
                permissions=permissions,
            )
        )
    return tuple(grants)
