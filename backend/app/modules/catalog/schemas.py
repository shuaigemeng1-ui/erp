"""SPU / SKU 请求与响应契约；金额使用 Decimal，成本响应按权限拆分。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STATUS_PATTERN = "^(active|archived)$"
SALES_STATUS_PATTERN = "^(on_sale|discontinued|trial|clearance)$"
MAX_IMAGE_URL_LENGTH = 500


def _validate_image_urls(urls: list[str]) -> list[str]:
    result = []
    for url in urls:
        value = (url or "").strip()
        if not value or len(value) > MAX_IMAGE_URL_LENGTH:
            raise ValueError("图片地址不能为空且长度不能超过 500")
        result.append(value)
    return result


class CatalogModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, coerce_numbers_to_str=True)


class ProductBusinessFields(CatalogModel):
    name_en: str | None = Field(default=None, max_length=200)
    preferred_supplier_id: str | None = None
    purchaser_id: str | None = None
    developer_id: str | None = None
    inquiry_buyer_id: str | None = None
    hs_code: str | None = Field(default=None, max_length=32)
    material_zh: str | None = Field(default=None, max_length=500)
    material_en: str | None = Field(default=None, max_length=500)
    usage: str | None = Field(default=None, max_length=1000)
    feature_tags: list[str] | None = Field(default=None, max_length=100)
    packaging_name: str | None = Field(default=None, max_length=100)
    length_cm: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    width_cm: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    height_cm: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    package_length_cm: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    package_width_cm: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    package_height_cm: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    sales_type: Literal["normal", "variation", "bundle", "assembly"] | None = None
    remark: str | None = Field(default=None, max_length=10000)
    features: str | None = Field(default=None, max_length=10000)

    @field_validator("preferred_supplier_id", "purchaser_id", "developer_id", "inquiry_buyer_id")
    @classmethod
    def validate_id(cls, value: str | None) -> str | None:
        if value is not None and (not value.isdigit() or int(value) <= 0):
            raise ValueError("关联ID必须为正整数")
        return value

    @field_validator("feature_tags")
    @classmethod
    def validate_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 100 for item in cleaned):
            raise ValueError("标签不能为空且长度不能超过100")
        return list(dict.fromkeys(cleaned))


class SkuBusinessFields(CatalogModel):
    image_url: str | None = Field(default=None, max_length=500)
    guidance_cost: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    average_cost: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    weight_g: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    sales_status: str | None = Field(default=None, pattern=SALES_STATUS_PATTERN)
    aliases: list[str] | None = Field(default=None, max_length=100)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        result = [item.strip() for item in values]
        if any(not item or len(item) > 128 for item in result):
            raise ValueError("别名不能为空且长度不能超过128")
        normalized = [item.casefold() for item in result]
        if len(normalized) != len(set(normalized)):
            raise ValueError("别名不能重复")
        return result


class ProductSkuCreate(SkuBusinessFields):
    sku_code: str = Field(min_length=1, max_length=64, description="SKU编号")
    name: str = Field(default="", max_length=200)
    price: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=4)
    cost_amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    specs: dict[str, str] | None = None
    sales_status: str = Field(default="on_sale", pattern=SALES_STATUS_PATTERN)
    aliases: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("specs")
    @classmethod
    def validate_specs(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _clean_specs(value)


def _clean_specs(value: dict[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    if len(value) > 30:
        raise ValueError("规格不能超过30项")
    result = {}
    for raw_key, raw_value in value.items():
        key, item = raw_key.strip(), raw_value.strip()
        if not key or not item or len(key) > 100 or len(item) > 200:
            raise ValueError("规格名称和值不能为空且长度不能超限")
        if key in result:
            raise ValueError("规格名称不能重复")
        result[key] = item
    return result


class ProductCreate(ProductBusinessFields):
    code: str = Field(min_length=1, max_length=64, description="SPU编号")
    name: str = Field(min_length=1, max_length=200, description="商品中文名称")
    brand: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=100)
    cost_amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    cost_currency: str = Field(default="CNY", min_length=1, max_length=8)
    base_price: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=4)
    description: str | None = Field(default=None, max_length=10000)
    sales_type: Literal["normal", "variation", "bundle", "assembly"] = "normal"
    skus: list[ProductSkuCreate] = Field(min_length=1, max_length=200)
    images: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("images")
    @classmethod
    def validate_images(cls, value: list[str]) -> list[str]:
        return _validate_image_urls(value)

    @model_validator(mode="after")
    def unique_codes(self) -> ProductCreate:
        codes = [item.sku_code.casefold() for item in self.skus]
        if len(codes) != len(set(codes)):
            raise ValueError("SKU编号在请求内不能重复")
        return self


class ProductSkuUpdate(SkuBusinessFields):
    id: str | None = None
    version: int | None = Field(default=None, ge=1)
    sku_code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    price: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    cost_amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    specs: dict[str, str] | None = None
    status: str | None = Field(default=None, pattern=STATUS_PATTERN)

    @field_validator("specs")
    @classmethod
    def validate_specs(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _clean_specs(value)


class ProductUpdate(ProductBusinessFields):
    version: int | None = Field(default=None, ge=1)
    code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    brand: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=100)
    cost_amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    cost_currency: str | None = Field(default=None, min_length=1, max_length=8)
    base_price: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    description: str | None = Field(default=None, max_length=10000)
    status: str | None = Field(default=None, pattern=STATUS_PATTERN)
    skus: list[ProductSkuUpdate] | None = Field(default=None, max_length=200)
    images: list[str] | None = Field(default=None, max_length=50)

    @field_validator("images")
    @classmethod
    def validate_images(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_image_urls(value)


class SummaryValue(BaseModel):
    state: Literal["uniform", "multiple", "missing", "partial_missing"]
    value: Decimal | None = None
    has_missing: bool = False


class CostSummary(BaseModel):
    current: SummaryValue
    guidance: SummaryValue
    average: SummaryValue


class SkuAliasOut(BaseModel):
    id: str
    identifier: str
    is_active: bool


class ProductImageOut(BaseModel):
    id: str
    url: str
    sort_order: int
    sku_id: str | None = None


class ProductSkuPublicOut(BaseModel):
    id: str
    sku_code: str
    name: str
    specs: dict[str, str] | None = None
    price: Decimal
    status: str
    image_url: str | None = None
    weight_g: Decimal | None = None
    sales_status: str = "on_sale"
    version: int = 1
    aliases: list[SkuAliasOut] = Field(default_factory=list)


class ProductSkuOut(ProductSkuPublicOut):
    cost_amount: Decimal | None = None
    guidance_cost: Decimal | None = None
    average_cost: Decimal | None = None


class ProductBaseOut(ProductBusinessFields):
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
    sales_type: Literal["normal", "variation", "bundle", "assembly"] = "normal"
    version: int = 1
    sales_status: str = "on_sale"
    weight_summary: SummaryValue | None = None
    primary_image: str | None = None
    preferred_supplier_name: str | None = None
    purchaser_name: str | None = None
    developer_name: str | None = None
    inquiry_buyer_name: str | None = None


class ProductListItemPublic(ProductBaseOut):
    sku_count: int = 0


class ProductListItem(ProductListItemPublic):
    cost_amount: Decimal | None = None
    cost_currency: str = "CNY"
    cost_summary: CostSummary | None = None


class ProductDetailPublic(ProductBaseOut):
    skus: list[ProductSkuPublicOut] = Field(default_factory=list)
    images: list[ProductImageOut] = Field(default_factory=list)


class ProductDetail(ProductDetailPublic):
    cost_amount: Decimal | None = None
    cost_currency: str = "CNY"
    cost_summary: CostSummary | None = None
    skus: list[ProductSkuOut] = Field(default_factory=list)


__all__ = ["ProductSkuCreate", "ProductCreate", "ProductSkuUpdate", "ProductUpdate", "ProductImageOut", "ProductSkuPublicOut", "ProductSkuOut", "ProductBaseOut", "ProductListItemPublic", "ProductListItem", "ProductDetailPublic", "ProductDetail", "SummaryValue", "CostSummary", "SkuAliasOut"]
