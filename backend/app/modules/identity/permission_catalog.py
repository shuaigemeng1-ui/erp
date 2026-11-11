"""权限编码目录（分组 + 中文名）。

``GET /tenants/{tenant_id}/permissions`` 与角色编辑器使用本目录；
权限编码本身来自 ``app/core/permissions.py``（稳定契约），本模块只提供展示元数据。
``permission_codes()`` 必须与 ``ALL_PERMISSIONS`` 完全一致，由单元测试守护。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.permissions import ALL_PERMISSIONS


@dataclass(frozen=True, slots=True)
class PermissionItem:
    """单个权限编码与中文名。"""

    code: str
    name: str


@dataclass(frozen=True, slots=True)
class PermissionGroup:
    """权限分组。"""

    code: str
    name: str
    items: tuple[PermissionItem, ...]


PERMISSION_GROUPS: tuple[PermissionGroup, ...] = (
    PermissionGroup(
        code="catalog",
        name="商品",
        items=(
            PermissionItem("catalog.product.read", "查看商品"),
            PermissionItem("catalog.product.create", "新建商品"),
            PermissionItem("catalog.product.update", "编辑与归档商品"),
            PermissionItem("catalog.cost.read", "查看与维护成本"),
        ),
    ),
    PermissionGroup(
        code="inventory",
        name="库存",
        items=(
            PermissionItem("inventory.read", "查看库存与流水"),
            PermissionItem("inventory.adjust", "人工调整库存"),
        ),
    ),
    PermissionGroup(
        code="channel",
        name="店铺与平台商品",
        items=(
            PermissionItem("store.read", "查看店铺"),
            PermissionItem("store.connect", "发起店铺授权与断开"),
            PermissionItem("channel.listing.read", "查看平台商品"),
            PermissionItem("channel.listing.map", "同步与映射平台商品"),
        ),
    ),
    PermissionGroup(
        code="order",
        name="订单与库存推送",
        items=(
            PermissionItem("order.read", "查看订单"),
            PermissionItem("order.sync", "发起订单同步"),
            PermissionItem("stock.push", "推送平台可售库存"),
        ),
    ),
    PermissionGroup(
        code="member",
        name="成员与授权",
        items=(
            PermissionItem("member.read", "查看成员"),
            PermissionItem("member.invite", "邀请与停用成员"),
            PermissionItem("role.manage", "管理角色与权限"),
            PermissionItem("role.assign", "分配角色与数据范围"),
        ),
    ),
    PermissionGroup(
        code="ops",
        name="审计、任务与设置",
        items=(
            PermissionItem("audit.read", "查看审计日志"),
            PermissionItem("task.read", "查看任务记录"),
            PermissionItem("tenant.setting.manage", "管理租户设置"),
        ),
    ),
)


def permission_items() -> tuple[PermissionItem, ...]:
    """展开后的全部权限项。"""
    return tuple(item for group in PERMISSION_GROUPS for item in group.items)


def permission_codes() -> frozenset[str]:
    """目录中出现的全部权限编码。"""
    return frozenset(item.code for item in permission_items())


def permission_name(code: str) -> str:
    """权限中文名；未知编码返回编码本身，便于前端展示新增权限。"""
    for item in permission_items():
        if item.code == code:
            return item.name
    return code


def catalogue() -> list[dict[str, object]]:
    """路由响应结构：``[{code, name, permissions: [{code, name}]}]``。"""
    return [
        {
            "code": group.code,
            "name": group.name,
            "permissions": [{"code": item.code, "name": item.name} for item in group.items],
        }
        for group in PERMISSION_GROUPS
    ]


def missing_catalogue_codes() -> frozenset[str]:
    """``ALL_PERMISSIONS`` 中尚未编入目录的编码（应为空）。"""
    return ALL_PERMISSIONS - permission_codes()


def unknown_catalogue_codes() -> frozenset[str]:
    """目录中超出 ``ALL_PERMISSIONS`` 的编码（应为空）。"""
    return permission_codes() - ALL_PERMISSIONS
