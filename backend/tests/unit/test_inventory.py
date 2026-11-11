"""商品 / 库存模块纯逻辑单元测试（不需要数据库、Redis、网络）。

覆盖（对应 plan.txt §3-§4 与验收项 2/3/6/8）：

1. 库存差额计算与人工调整禁止负库存（``allow_negative`` 语义）；
2. 数据范围：库存地点维度过滤、空范围无授权、「局部编辑 + 全局查看」不能合成全局编辑；
3. 成本字段：无 ``catalog.cost.read`` 权限时在响应模型层面不暴露成本；
4. 模型规范：六张租户库表零外键、表 / 字段中文注释、金额 DECIMAL 精度；
5. 请求模型白名单：调整数量为 0、SKU 编码重复等被 422 拦截。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.access import EffectivePermissions, ScopeGrant
from app.core.errors import AppError, ErrorCode, ScopeDeniedError
from app.core.permissions import (
    SCOPE_ALL,
    SCOPE_STOCK_PLACES,
    SCOPE_STORES,
    Permission,
)
from app.db.base import TenantBase
from app.modules.catalog import models_tenant as catalog_models
from app.modules.catalog.schemas import (
    ProductCreate,
    ProductDetailPublic,
    ProductListItem,
    ProductListItemPublic,
    ProductSkuCreate,
    ProductSkuOut,
    ProductSkuPublicOut,
)
from app.modules.catalog.service import can_read_cost, redact_cost
from app.modules.inventory import models_tenant as inventory_models
from app.modules.inventory.schemas import InventoryAdjustRequest
from app.modules.inventory.service import (
    MovementType,
    available_after,
    ensure_available_non_negative,
    ensure_stock_place_in_scope,
    scope_allowed_stock_places,
)

NOW = datetime(2026, 1, 1, 0, 0, 0)


# --------------------------------------------------------------------------- 工具
def _grant(
    *,
    permissions: set[str] | frozenset[str],
    scope_type: str = SCOPE_ALL,
    scope_ids: frozenset[int] = frozenset(),
    role_binding_id: int = 1,
    role_id: int = 1,
) -> ScopeGrant:
    return ScopeGrant(
        role_binding_id=role_binding_id,
        role_id=role_id,
        scope_type=scope_type,
        scope_ids=scope_ids,
        permissions=frozenset(permissions),
    )


def _effective(*grants: ScopeGrant) -> EffectivePermissions:
    return EffectivePermissions(grants=tuple(grants))


# --------------------------------------------------------------------------- 库存差额与负库存
def test_available_after_positive_and_negative_change() -> None:
    assert available_after(10, 5) == 15
    assert available_after(10, -4) == 6
    assert available_after(0, -3) == -3


def test_manual_adjust_negative_forbidden() -> None:
    with pytest.raises(AppError) as exc_info:
        ensure_available_non_negative(-1, allow_negative=False)
    error = exc_info.value
    assert error.code == ErrorCode.INVENTORY_NEGATIVE_FORBIDDEN
    assert error.status_code == 409
    assert error.details == {"available_quantity": -1}


def test_manual_adjust_zero_balance_allowed() -> None:
    ensure_available_non_negative(0, allow_negative=False)


def test_external_order_may_go_negative() -> None:
    # 外部已成立订单即使造成缺货也必须保存，负库存由业务标记异常
    ensure_available_non_negative(-5, allow_negative=True)


# --------------------------------------------------------------------------- 数据范围
def test_scope_all_means_all_stock_places() -> None:
    effective = _effective(_grant(permissions={Permission.INVENTORY_READ}))
    assert scope_allowed_stock_places(effective, Permission.INVENTORY_READ) is None


def test_scope_specific_stock_places_union() -> None:
    effective = _effective(
        _grant(
            permissions={Permission.INVENTORY_READ},
            scope_type=SCOPE_STOCK_PLACES,
            scope_ids=frozenset({1, 2}),
            role_binding_id=1,
        ),
        _grant(
            permissions={Permission.INVENTORY_READ},
            scope_type=SCOPE_STOCK_PLACES,
            scope_ids=frozenset({2, 3}),
            role_binding_id=2,
        ),
    )
    assert scope_allowed_stock_places(effective, Permission.INVENTORY_READ) == frozenset(
        {1, 2, 3}
    )


def test_empty_scope_grants_nothing() -> None:
    effective = _effective()
    assert scope_allowed_stock_places(effective, Permission.INVENTORY_READ) == frozenset()
    assert not effective.covers(Permission.INVENTORY_READ, stock_place_id=1)


def test_store_scope_does_not_grant_stock_place_scope() -> None:
    """店铺范围与库存地点范围是两个维度，不能互相替代。"""
    effective = _effective(
        _grant(
            permissions={Permission.INVENTORY_READ},
            scope_type=SCOPE_STORES,
            scope_ids=frozenset({100}),
        )
    )
    assert scope_allowed_stock_places(effective, Permission.INVENTORY_READ) == frozenset()
    assert not effective.covers(Permission.INVENTORY_READ, stock_place_id=1)


def test_local_edit_plus_global_read_cannot_become_global_edit() -> None:
    """「库存地点 1 编辑」+「全部查看」只能编辑地点 1，不能合成为全局编辑。"""
    effective = _effective(
        _grant(
            permissions={Permission.INVENTORY_ADJUST},
            scope_type=SCOPE_STOCK_PLACES,
            scope_ids=frozenset({1}),
            role_binding_id=1,
        ),
        _grant(
            permissions={Permission.INVENTORY_READ},
            scope_type=SCOPE_ALL,
            role_binding_id=2,
        ),
    )
    ensure_stock_place_in_scope(effective, Permission.INVENTORY_ADJUST, 1)
    with pytest.raises(ScopeDeniedError) as exc_info:
        ensure_stock_place_in_scope(effective, Permission.INVENTORY_ADJUST, 2)
    assert exc_info.value.status_code == 403


def test_adjust_without_permission_is_denied() -> None:
    effective = _effective(_grant(permissions={Permission.INVENTORY_READ}))
    with pytest.raises(ScopeDeniedError):
        ensure_stock_place_in_scope(effective, Permission.INVENTORY_ADJUST, 1)


def test_stock_place_scope_only_applies_to_its_permission() -> None:
    effective = _effective(
        _grant(
            permissions={Permission.INVENTORY_READ},
            scope_type=SCOPE_STOCK_PLACES,
            scope_ids=frozenset({7}),
        )
    )
    assert effective.covers(Permission.INVENTORY_READ, stock_place_id=7)
    assert not effective.covers(Permission.INVENTORY_READ, stock_place_id=8)


# --------------------------------------------------------------------------- 成本字段过滤
def test_cost_visible_only_with_permission() -> None:
    without_cost = _effective(_grant(permissions={Permission.CATALOG_PRODUCT_READ}))
    with_cost = _effective(
        _grant(
            permissions={
                Permission.CATALOG_PRODUCT_READ,
                Permission.CATALOG_COST_READ,
            }
        )
    )
    assert can_read_cost(without_cost) is False
    assert can_read_cost(with_cost) is True
    assert redact_cost(Decimal("12.3456"), can_read=False) is None
    assert redact_cost(Decimal("12.3456"), can_read=True) == Decimal("12.3456")


def test_public_product_models_do_not_expose_cost() -> None:
    item = ProductListItemPublic(
        id="1",
        code="SKU-1",
        name="测试商品",
        status="active",
        base_price=Decimal("99.0000"),
        created_at=NOW,
        updated_at=NOW,
        sku_count=2,
    )
    dumped = item.model_dump()
    assert "cost_amount" not in dumped
    assert "cost_currency" not in dumped

    sku = ProductSkuPublicOut(
        id="2",
        sku_code="SKU-1-RED",
        name="红色",
        price=Decimal("9.9000"),
        status="active",
    )
    detail = ProductDetailPublic(
        id="1",
        code="SKU-1",
        name="测试商品",
        status="active",
        base_price=Decimal("99.0000"),
        created_at=NOW,
        updated_at=NOW,
        skus=[sku],
    )
    dumped_detail = detail.model_dump()
    assert "cost_amount" not in dumped_detail
    assert "cost_amount" not in dumped_detail["skus"][0]


def test_authorized_product_models_include_cost() -> None:
    item = ProductListItem(
        id="1",
        code="SKU-1",
        name="测试商品",
        status="active",
        base_price=Decimal("99.0000"),
        created_at=NOW,
        updated_at=NOW,
        sku_count=1,
        cost_amount=Decimal("3.5000"),
        cost_currency="CNY",
    )
    assert item.model_dump()["cost_amount"] == Decimal("3.5000")

    sku = ProductSkuOut(
        id="2",
        sku_code="SKU-1-RED",
        name="红色",
        price=Decimal("9.9000"),
        status="active",
        cost_amount=Decimal("1.2000"),
    )
    assert sku.model_dump()["cost_amount"] == Decimal("1.2000")


def test_ids_are_serialized_as_strings() -> None:
    item = ProductListItemPublic(
        id="9007199254740993",
        code="SKU-1",
        name="测试商品",
        status="active",
        base_price=Decimal("1.0000"),
        created_at=NOW,
        updated_at=NOW,
    )
    assert item.model_dump()["id"] == "9007199254740993"


# --------------------------------------------------------------------------- 请求模型白名单
def test_adjust_request_rejects_zero_quantity() -> None:
    with pytest.raises(ValidationError):
        InventoryAdjustRequest(sku_id=1, stock_place_id=1, quantity_change=0)


def test_adjust_request_accepts_negative_quantity_and_limits_note() -> None:
    payload = InventoryAdjustRequest(
        sku_id=1, stock_place_id=1, quantity_change=-3, note="盘亏"
    )
    assert payload.quantity_change == -3
    with pytest.raises(ValidationError):
        InventoryAdjustRequest(
            sku_id=1, stock_place_id=1, quantity_change=-3, note="x" * 501
        )


def test_product_create_rejects_duplicate_sku_codes() -> None:
    with pytest.raises(ValidationError):
        ProductCreate(
            code="P-1",
            name="商品",
            base_price=Decimal("10"),
            skus=[
                ProductSkuCreate(sku_code="S-1", name="规格1", price=Decimal("10")),
                ProductSkuCreate(sku_code="s-1", name="规格2", price=Decimal("10")),
            ],
        )


def test_product_create_requires_at_least_one_sku() -> None:
    with pytest.raises(ValidationError):
        ProductCreate(
            code="P-1",
            name="商品",
            base_price=Decimal("10"),
            skus=[],
        )


def test_product_create_rejects_invalid_image_url() -> None:
    with pytest.raises(ValidationError):
        ProductCreate(
            code="P-1",
            name="商品",
            base_price=Decimal("10"),
            skus=[ProductSkuCreate(sku_code="S-1", name="规格1", price=Decimal("10"))],
            images=["x" * 501],
        )


# --------------------------------------------------------------------------- 模型规范
OUR_TABLES = (
    "products",
    "product_skus",
    "product_images",
    "stock_places",
    "inventory_balances",
    "stock_movements",
)


def _our_tenant_tables() -> list:
    return [TenantBase.metadata.tables[name] for name in OUR_TABLES]


def test_catalog_and_inventory_tables_registered() -> None:
    assert catalog_models.Product.__tablename__ == "products"
    assert catalog_models.ProductSku.__tablename__ == "product_skus"
    assert catalog_models.ProductImage.__tablename__ == "product_images"
    assert inventory_models.StockPlace.__tablename__ == "stock_places"
    assert inventory_models.InventoryBalance.__tablename__ == "inventory_balances"
    assert inventory_models.StockMovement.__tablename__ == "stock_movements"
    assert set(OUR_TABLES) <= set(TenantBase.metadata.tables)


def test_our_tables_have_no_foreign_keys() -> None:
    for table in _our_tenant_tables():
        assert not table.foreign_keys, f"{table.name} 不允许声明外键"
        assert not table.foreign_key_constraints, f"{table.name} 不允许声明外键约束"


def test_our_tables_and_columns_have_chinese_comments() -> None:
    def has_chinese(value: str) -> bool:
        return any("\u4e00" <= char <= "\u9fff" for char in value)

    for table in _our_tenant_tables():
        assert has_chinese(table.comment or ""), f"表 {table.name} 缺少中文注释"
        for column in table.columns:
            assert has_chinese(column.comment or ""), (
                f"字段 {table.name}.{column.name} 缺少中文注释"
            )


def test_business_unique_constraints_registered() -> None:
    expected = {
        "products": {"tenant_id", "code"},
        "product_skus": {"tenant_id", "sku_code"},
        "stock_places": {"tenant_id", "code"},
        "inventory_balances": {"tenant_id", "sku_id", "stock_place_id"},
    }
    for table_name, columns in expected.items():
        table = TenantBase.metadata.tables[table_name]
        found = {
            frozenset(constraint.columns.keys())
            for constraint in table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }
        assert frozenset(columns) in found, f"{table_name} 缺少唯一约束 {sorted(columns)}"


def test_money_columns_use_decimal_18_4() -> None:
    for table_name, column_name in (
        ("products", "cost_amount"),
        ("products", "base_price"),
        ("product_skus", "price"),
        ("product_skus", "cost_amount"),
    ):
        column = TenantBase.metadata.tables[table_name].c[column_name]
        assert column.type.precision == 18
        assert column.type.scale == 4


def test_inventory_balance_version_column_exists() -> None:
    column = TenantBase.metadata.tables["inventory_balances"].c["version"]
    assert column.nullable is False
    assert column.server_default is not None


def test_movement_type_constants_are_stable() -> None:
    assert MovementType.MANUAL_ADJUST == "manual_adjust"
    assert MovementType.ORDER_DEDUCT == "order_deduct"
    assert MovementType.ORDER_RESTORE == "order_restore"
