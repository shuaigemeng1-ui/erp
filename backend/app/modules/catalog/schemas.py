"""商品模块请求 / 响应模型。

约定：

* 修改字段用请求模型白名单控制；服务端只接受本文件显式声明的字段。
* 响应中的大整数 ID 一律字符串（``str``），避免前端浮点精度丢失。
* 成本字段（``cost_amount`` / ``cost_currency``）拆分为两套响应模型：
  无 ``catalog.cost.read`` 权限时使用不带成本字段的 ``*PublicOut``，
  从响应模型层面保证成本不会被序列化出去。金额使用 ``Decimal``。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STATUS_PATTERN = "^(active|archived)$"
MAX_IMAGE_URL_LENGTH = 500


def _validate_image_urls(urls: list[str]) -> list[str]:
    cleaned: list[str] = []
    for url in urls:
        value = (url or "").strip()
        if not value:
            raise ValueError("图片地址不能为空")
        if len(value) > MAX_IMAGE_URL_LENGTH:
            raise ValueError(f"图片地址长度不能超过 {MAX_IMAGE_URL_LENGTH}")
        cleaned.append(value)
    return cleaned


# --------------------------------------------------------------------------- 请求模型
class ProductSkuCreate(BaseModel):
    """创建商品时同时创建的 SKU。无规格商品也必须有一个 SKU。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    sku_code: str = Field(min_length=1, max_length=64, description="SKU 编码（租户内唯一）")
    name: str = Field(min_length=1, max_length=200, description="SKU 名称")
    price: Decimal = Field(ge=0, description="SKU 售价")
    cost_amount: Decimal | None = Field(
        default=None, ge=0, description="SKU 成本金额；无 catalog.cost.read 权限时禁止传递"
    )
    specs: dict[str, Any] | None = Field(default=None, description="规格 JSON（颜色、尺码等）")


class ProductCreate(BaseModel):
    """新建商品请求。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    code: str = Field(min_length=1, max_length=64, description="商品编码（租户内唯一）")
    name: str = Field(min_length=1, max_length=200, description="商品名称")
    brand: str | None = Field(default=None, max_length=100, description="品牌")
    category: str | None = Field(default=None, max_length=100, description="分类")
    cost_amount: Decimal | None = Field(
        default=None, ge=0, description="成本金额；无 catalog.cost.read 权限时禁止传递"
    )
    cost_currency: str = Field(default="CNY", min_length=1, max_length=8, description="成本币种")
    base_price: Decimal = Field(ge=0, description="基础售价")
    description: str | None = Field(default=None, max_length=2000, description="商品描述")
    skus: list[ProductSkuCreate] = Field(
        min_length=1, max_length=200, description="SKU 列表，至少 1 个"
    )
    images: list[str] = Field(default_factory=list, max_length=50, description="商品图片地址列表")

    @field_validator("images")
    @classmethod
    def _check_images(cls, value: list[str]) -> list[str]:
        return _validate_image_urls(value)

    @model_validator(mode="after")
    def _check_unique_sku_codes(self) -> "ProductCreate":
        codes = [item.sku_code.casefold() for item in self.skus]
        if len(codes) != len(set(codes)):
            raise ValueError("SKU 编码在请求内不能重复")
        return self


class ProductSkuUpdate(BaseModel):
    """局部更新 SKU：带 ``id`` 表示更新已有 SKU，不带表示新增。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    id: str | None = Field(default=None, description="已有 SKU 的 ID（字符串）")
    sku_code: str | None = Field(default=None, min_length=1, max_length=64, description="SKU 编码")
    name: str | None = Field(default=None, min_length=1, max_length=200, description="SKU 名称")
    price: Decimal | None = Field(default=None, ge=0, description="SKU 售价")
    cost_amount: Decimal | None = Field(
        default=None, ge=0, description="SKU 成本金额；无权限时禁止传递"
    )
    specs: dict[str, Any] | None = Field(default=None, description="规格 JSON")
    status: str | None = Field(default=None, pattern=STATUS_PATTERN, description="SKU 状态")


class ProductUpdate(BaseModel):
    """局部更新商品：仅处理 ``model_fields_set`` 中显式出现的字段。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    code: str | None = Field(default=None, min_length=1, max_length=64, description="商品编码")
    name: str | None = Field(default=None, min_length=1, max_length=200, description="商品名称")
    brand: str | None = Field(default=None, max_length=100, description="品牌")
    category: str | None = Field(default=None, max_length=100, description="分类")
    cost_amount: Decimal | None = Field(
        default=None, ge=0, description="成本金额；无 catalog.cost.read 权限时禁止传递"
    )
    cost_currency: str | None = Field(
        default=None, min_length=1, max_length=8, description="成本币种"
    )
    base_price: Decimal | None = Field(default=None, ge=0, description="基础售价")
    description: str | None = Field(default=None, max_length=2000, description="商品描述")
    status: str | None = Field(default=None, pattern=STATUS_PATTERN, description="商品状态")
    skus: list[ProductSkuUpdate] | None = Field(
        default=None, max_length=200, description="SKU 更新列表；只更新传入项，不删除未传入项"
    )
    images: list[str] | None = Field(
        default=None,
        max_length=50,
        description="图片列表；显式传 null 或 [] 表示清空，不传表示不修改",
    )

    @field_validator("images")
    @classmethod
    def _check_images(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_image_urls(value)


# --------------------------------------------------------------------------- 响应模型
class ProductImageOut(BaseModel):
    """商品图片输出。"""

    id: str
    url: str
    sort_order: int
    sku_id: str | None = None


class ProductSkuPublicOut(BaseModel):
    """无成本字段的 SKU 输出（缺少 catalog.cost.read 权限时使用）。"""

    id: str
    sku_code: str
    name: str
    specs: dict[str, Any] | None = None
    price: Decimal
    status: str


class ProductSkuOut(ProductSkuPublicOut):
    """含成本字段的 SKU 输出（拥有 catalog.cost.read 权限时使用）。"""

    cost_amount: Decimal | None = None


class ProductBaseOut(BaseModel):
    """商品基础输出。"""

    id: str
    code: str
    name: str
    brand: str | None = None
    category: str | None = None
    status: str
    base_price: Decimal
    description: str | None = None
    created_at: datetime
    updated_at: datetime


class ProductListItemPublic(ProductBaseOut):
    """商品列表项（无成本字段）。"""

    sku_count: int = 0


class ProductListItem(ProductListItemPublic):
    """商品列表项（含成本字段）。"""

    cost_amount: Decimal | None = None
    cost_currency: str = "CNY"


class ProductDetailPublic(ProductBaseOut):
    """商品详情（无成本字段）。"""

    skus: list[ProductSkuPublicOut] = Field(default_factory=list)
    images: list[ProductImageOut] = Field(default_factory=list)


class ProductDetail(ProductDetailPublic):
    """商品详情（含成本字段）。"""

    cost_amount: Decimal | None = None
    cost_currency: str = "CNY"
    skus: list[ProductSkuOut] = Field(default_factory=list)


__all__ = [
    "ProductSkuCreate",
    "ProductCreate",
    "ProductSkuUpdate",
    "ProductUpdate",
    "ProductImageOut",
    "ProductSkuPublicOut",
    "ProductSkuOut",
    "ProductBaseOut",
    "ProductListItemPublic",
    "ProductListItem",
    "ProductDetailPublic",
    "ProductDetail",
]
