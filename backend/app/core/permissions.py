"""功能权限编码与预置角色定义。

权限编码是稳定契约：前后端、审计、角色初始化都引用这里，禁止散落字符串。
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    """首版功能权限编码。"""

    # 商品
    CATALOG_PRODUCT_READ = "catalog.product.read"
    CATALOG_PRODUCT_CREATE = "catalog.product.create"
    CATALOG_PRODUCT_UPDATE = "catalog.product.update"
    CATALOG_COST_READ = "catalog.cost.read"
    # 库存
    INVENTORY_READ = "inventory.read"
    INVENTORY_ADJUST = "inventory.adjust"
    # 店铺与渠道
    STORE_CONNECT = "store.connect"
    STORE_READ = "store.read"
    CHANNEL_LISTING_READ = "channel.listing.read"
    CHANNEL_LISTING_MAP = "channel.listing.map"
    # 订单
    ORDER_READ = "order.read"
    ORDER_SYNC = "order.sync"
    # 库存推送
    STOCK_PUSH = "stock.push"
    # 成员与授权
    MEMBER_READ = "member.read"
    MEMBER_INVITE = "member.invite"
    ROLE_MANAGE = "role.manage"
    ROLE_ASSIGN = "role.assign"
    # 审计与任务
    AUDIT_READ = "audit.read"
    TASK_READ = "task.read"
    # 租户设置
    TENANT_SETTING_MANAGE = "tenant.setting.manage"


ALL_PERMISSIONS: frozenset[str] = frozenset(item.value for item in Permission)

# 数据范围类型
SCOPE_ALL = "all"
SCOPE_STORES = "stores"
SCOPE_STOCK_PLACES = "stock_places"
SCOPE_TYPES: tuple[str, ...] = (SCOPE_ALL, SCOPE_STORES, SCOPE_STOCK_PLACES)


class PlatformRole(StrEnum):
    """平台侧角色（存中心库）。"""

    PLATFORM_ADMIN = "platform_admin"
    PLATFORM_OPS = "platform_ops"


class PredefinedRoleCode(StrEnum):
    """租户内预置角色编码。租户可以复制和修改，编码保持不变用于升级对照。"""

    OWNER = "owner"
    ADMIN = "admin"
    CATALOG_OPERATOR = "catalog_operator"
    ORDER_OPERATOR = "order_operator"
    STOCK_OPERATOR = "stock_operator"
    READONLY = "readonly"


# 预置角色 -> 权限集合。owner/admin 显式列出全部权限，避免升级后遗漏。
PREDEFINED_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    PredefinedRoleCode.OWNER: ALL_PERMISSIONS,
    PredefinedRoleCode.ADMIN: ALL_PERMISSIONS,
    PredefinedRoleCode.CATALOG_OPERATOR: frozenset(
        {
            Permission.CATALOG_PRODUCT_READ,
            Permission.CATALOG_PRODUCT_CREATE,
            Permission.CATALOG_PRODUCT_UPDATE,
            Permission.CATALOG_COST_READ,
            Permission.INVENTORY_READ,
            Permission.CHANNEL_LISTING_READ,
            Permission.CHANNEL_LISTING_MAP,
            Permission.STORE_READ,
            Permission.TASK_READ,
        }
    ),
    PredefinedRoleCode.ORDER_OPERATOR: frozenset(
        {
            Permission.ORDER_READ,
            Permission.ORDER_SYNC,
            Permission.STOCK_PUSH,
            Permission.CATALOG_PRODUCT_READ,
            Permission.INVENTORY_READ,
            Permission.STORE_READ,
            Permission.CHANNEL_LISTING_READ,
            Permission.TASK_READ,
        }
    ),
    PredefinedRoleCode.STOCK_OPERATOR: frozenset(
        {
            Permission.INVENTORY_READ,
            Permission.INVENTORY_ADJUST,
            Permission.STOCK_PUSH,
            Permission.CATALOG_PRODUCT_READ,
            Permission.STORE_READ,
            Permission.TASK_READ,
        }
    ),
    PredefinedRoleCode.READONLY: frozenset(
        {
            Permission.CATALOG_PRODUCT_READ,
            Permission.INVENTORY_READ,
            Permission.ORDER_READ,
            Permission.STORE_READ,
            Permission.CHANNEL_LISTING_READ,
            Permission.MEMBER_READ,
            Permission.AUDIT_READ,
            Permission.TASK_READ,
        }
    ),
}

# 兼容旧命名（role.permissions 字段内使用的字符串集合）
PERMISSION_CODES: tuple[str, ...] = tuple(sorted(ALL_PERMISSIONS))
