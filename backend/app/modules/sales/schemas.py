"""销售（订单）模块的 Pydantic 响应 / 请求模型。

约定：大整数 ID 序列化为字符串、金额 ``Decimal``、时间 UTC（见 docs/api-contract.md）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.modules.sales.models_tenant import (
    Order,
    OrderItem,
    OrderStatusHistory,
    OrderStockEffect,
)

IdStr = Annotated[int, PlainSerializer(lambda value: str(value), return_type=str, when_used="json")]


class OrderItemOut(BaseModel):
    """订单明细行。"""

    id: IdStr
    external_item_id: str = Field(description="平台商品ID")
    external_sku_id: str = Field(description="平台规格/卖家SKU")
    sku_id: IdStr | None = Field(default=None, description="本地SKU ID（未映射为空）")
    item_name: str = Field(default="", description="商品名称快照")
    quantity: int = Field(description="购买数量")
    unit_price: Decimal = Field(description="成交单价")
    discount_amount: Decimal = Field(default=Decimal("0"), description="优惠金额")
    item_status: str = Field(default="", description="平台明细状态")
    mapping_status: str = Field(description="SKU 映射状态（mapped/missing/ambiguous）")

    @classmethod
    def from_model(cls, item: OrderItem) -> "OrderItemOut":
        return cls(
            id=int(item.id),
            external_item_id=item.external_item_id or "",
            external_sku_id=item.external_sku_id or "",
            sku_id=int(item.sku_id) if item.sku_id else None,
            item_name=item.item_name or "",
            quantity=int(item.quantity or 0),
            unit_price=item.unit_price or Decimal("0"),
            discount_amount=item.discount_amount or Decimal("0"),
            item_status=item.item_status or "",
            mapping_status=item.mapping_status or "missing",
        )


class OrderStatusHistoryOut(BaseModel):
    """订单状态历史。"""

    id: IdStr
    from_status: str
    to_status: str
    platform_status: str
    source: str
    changed_at: datetime | None

    @classmethod
    def from_model(cls, row: OrderStatusHistory) -> "OrderStatusHistoryOut":
        return cls(
            id=int(row.id),
            from_status=row.from_status or "",
            to_status=row.to_status or "",
            platform_status=row.platform_status or "",
            source=row.source or "",
            changed_at=row.changed_at,
        )


class OrderStockEffectOut(BaseModel):
    """订单库存影响。"""

    id: IdStr
    order_item_id: IdStr
    sku_id: IdStr
    stock_place_id: IdStr
    applied_quantity: int = Field(description="已应用的库存影响数量")
    target_quantity: int = Field(description="目标库存影响数量")
    status: str = Field(description="影响状态（pending/applied/skipped/failed）")

    @classmethod
    def from_model(cls, effect: OrderStockEffect) -> "OrderStockEffectOut":
        return cls(
            id=int(effect.id),
            order_item_id=int(effect.order_item_id),
            sku_id=int(effect.sku_id),
            stock_place_id=int(effect.stock_place_id),
            applied_quantity=int(effect.applied_quantity or 0),
            target_quantity=int(effect.target_quantity or 0),
            status=effect.status or "pending",
        )


class OrderOut(BaseModel):
    """订单列表项。"""

    model_config = ConfigDict(from_attributes=True)

    id: IdStr
    store_id: IdStr
    channel: str
    external_order_no: str
    status: str = Field(description="归一化订单状态")
    platform_status: str = Field(description="平台原始状态")
    currency: str
    total_amount: Decimal
    buyer_name: str
    placed_at: datetime | None
    abnormal_reason: str = Field(description="异常原因（非空表示需人工处理）")
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_model(cls, order: Order) -> "OrderOut":
        return cls(
            id=int(order.id),
            store_id=int(order.store_id),
            channel=order.channel or "",
            external_order_no=order.external_order_no or "",
            status=order.status or "unknown",
            platform_status=order.platform_status or "",
            currency=order.currency or "",
            total_amount=order.total_amount or Decimal("0"),
            buyer_name=order.buyer_name or "",
            placed_at=order.placed_at,
            abnormal_reason=order.abnormal_reason or "",
            created_at=order.created_at,
            updated_at=order.updated_at,
        )


class OrderDetailOut(OrderOut):
    """订单详情：明细、状态历史、库存影响。"""

    items: list[OrderItemOut] = Field(default_factory=list)
    history: list[OrderStatusHistoryOut] = Field(default_factory=list)
    stock_effects: list[OrderStockEffectOut] = Field(default_factory=list)

class OrderSyncRequest(BaseModel):
    """手动同步订单请求：不传店铺表示同步当前用户有权限的全部店铺。"""

    store_id: int | None = Field(default=None, ge=1, description="店铺ID（可选）")
