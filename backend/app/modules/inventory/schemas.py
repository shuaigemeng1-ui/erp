"""库存模块请求 / 响应模型。

* 调整数量使用整数（``Integer`` 列），正数入库、负数出库，0 无意义直接 422。
* 大整数 ID 一律字符串输出；时间由 FastAPI 序列化为 ISO 8601（UTC）。
* 列表项带 SKU 编码 / 商品名 / 库存地点名等冗余展示字段，前端无需二次查询。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --------------------------------------------------------------------------- 请求模型
class InventoryAdjustRequest(BaseModel):
    """人工调整库存请求：只允许操作有权限的库存地点，且禁止产生负库存。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    sku_id: int = Field(gt=0, description="SKU ID")
    stock_place_id: int = Field(gt=0, description="库存地点 ID")
    quantity_change: int = Field(
        ge=-1_000_000_000,
        le=1_000_000_000,
        description="调整数量（正数入库，负数出库）",
    )
    note: str = Field(default="", max_length=500, description="调整备注（写入库存流水与审计）")

    @field_validator("quantity_change")
    @classmethod
    def _check_quantity_change(cls, value: int) -> int:
        if value == 0:
            raise ValueError("调整数量不能为 0")
        return value


# --------------------------------------------------------------------------- 响应模型
class StockPlaceOut(BaseModel):
    """库存地点输出。"""

    id: str
    code: str
    name: str
    is_default: bool
    status: str


class InventoryBalanceOut(BaseModel):
    """库存余额输出（含展示用冗余字段）。"""

    id: str
    sku_id: str
    sku_code: str | None = None
    product_id: str | None = None
    product_name: str | None = None
    stock_place_id: str
    stock_place_name: str | None = None
    available_quantity: int
    reserved_quantity: int
    version: int
    updated_at: datetime | None = None


class InventoryMovementOut(BaseModel):
    """库存流水输出。"""

    id: str
    sku_id: str
    sku_code: str | None = None
    product_name: str | None = None
    stock_place_id: str
    stock_place_name: str | None = None
    quantity_change: int
    balance_after: int
    movement_type: str
    reference_type: str = ""
    reference_id: str | None = None
    operator_id: str | None = None
    note: str = ""
    created_at: datetime | None = None


class InventoryAdjustResult(BaseModel):
    """人工调整结果：返回调整后可用数量，便于前端立即刷新。"""

    balance_id: str | None = None
    sku_id: str
    stock_place_id: str
    available_quantity: int


__all__ = [
    "InventoryAdjustRequest",
    "StockPlaceOut",
    "InventoryBalanceOut",
    "InventoryMovementOut",
    "InventoryAdjustResult",
]
