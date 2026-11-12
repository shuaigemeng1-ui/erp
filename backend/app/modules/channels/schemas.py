"""渠道模块 Pydantic 请求 / 响应模型。

约定（docs/api-contract.md）：

* 大整数 ID 一律字符串输出，避免前端 JS 精度丢失；
* 时间统一 UTC，输出 ISO 8601（``Z`` 结尾）；
* 店铺凭据只输出状态与过期时间，绝不输出 Token 明文；
* 请求模型使用白名单字段，不接受前端传入租户 ID / 店铺归属。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.modules.channels.shopee.signature import safe_redirect_path


# --------------------------------------------------------------------------- 工具
def id_string(value: int | str | None) -> str | None:
    """大整数 ID 转字符串；``None`` 保持 ``None``。"""
    if value is None:
        return None
    return str(int(value))


def utc_iso(value: datetime | None) -> str | None:
    """UTC 时间转 ISO 8601 字符串（``Z`` 结尾）。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat() + "Z"


def decimal_string(value: Decimal | None) -> str | None:
    """金额按字符串输出，避免浮点误差。"""
    if value is None:
        return None
    return format(value, "f")


def _require_positive_id(value: str) -> int:
    text = str(value or "").strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError("ID 必须为正整数字符串")
    return int(text)


# --------------------------------------------------------------------------- 请求
class ShopeeAuthorizeIn(BaseModel):
    """发起 Shopee 店铺授权。"""

    environment: Literal["production", "test"] | None = Field(
        default=None, description="应用环境，缺省按部署环境选择"
    )
    redirect_path: str | None = Field(
        default=None, max_length=500, description="授权成功后前端跳转路径（站内相对路径）"
    )

    @field_validator("redirect_path")
    @classmethod
    def _safe_redirect(cls, value: str | None) -> str | None:
        if value is None:
            return None
        safe = safe_redirect_path(value, "")
        if not safe:
            raise ValueError("redirect_path 必须是站内相对路径")
        return safe


class ListingSyncIn(BaseModel):
    """同步某店铺的平台商品（异步任务）。"""

    store_id: str = Field(description="店铺ID（字符串）")

    @field_validator("store_id", mode="before")
    @classmethod
    def _store_id(cls, value: object) -> str:
        _require_positive_id(str(value))
        return str(int(str(value).strip()))

    @property
    def store_id_int(self) -> int:
        return int(self.store_id)


class ListingLinkIn(BaseModel):
    """人工建立平台商品 → 本地 SKU 映射。"""

    product_id: str = Field(description="本地商品ID（字符串）")
    sku_id: str = Field(description="本地 SKU ID（字符串）")

    @field_validator("product_id", "sku_id", mode="before")
    @classmethod
    def _positive(cls, value: object) -> str:
        _require_positive_id(str(value))
        return str(int(str(value).strip()))

    @property
    def product_id_int(self) -> int:
        return int(self.product_id)

    @property
    def sku_id_int(self) -> int:
        return int(self.sku_id)


# --------------------------------------------------------------------------- 响应
class StoreOut(BaseModel):
    id: str = Field(description="店铺ID")
    channel: str = Field(description="渠道编码")
    store_code: str = Field(description="ERP 内部店铺编码")
    name: str = Field(description="店铺名称")
    external_store_id: str = Field(description="平台店铺ID")
    region: str | None = Field(default=None, description="站点区域")
    timezone: str = Field(description="店铺时区")
    status: str = Field(description="状态：connected/reauth_required/disconnected")
    environment: str = Field(description="应用环境")
    authorized_at: str | None = Field(default=None, description="最近授权时间（UTC）")
    last_synced_at: str | None = Field(default=None, description="最近同步时间（UTC）")
    disconnected_at: str | None = Field(default=None, description="断开授权时间（UTC）")
    last_error: str | None = Field(default=None, description="最近一次错误摘要")
    credential_status: str | None = Field(default=None, description="凭据状态")
    access_token_expires_at: str | None = Field(default=None, description="访问令牌过期时间（UTC）")
    refresh_token_expires_at: str | None = Field(default=None, description="刷新令牌过期时间（UTC）")


class StoreCreateIn(BaseModel):
    """新建店铺：先选渠道、填 ERP 显示名称，授权前状态为待授权。"""

    channel: Literal["shopee", "tiktok"] = Field(description="渠道：shopee / tiktok")
    name: str = Field(min_length=1, max_length=128, description="ERP 中显示的店铺名称")
    environment: Literal["production", "test"] | None = Field(
        default=None, description="应用环境，缺省按部署环境选择"
    )
    region: str | None = Field(default=None, max_length=16, description="站点区域，如 UK/US")


class AuthorizeOut(BaseModel):
    auth_url: str = Field(description="平台授权地址（一次性会话）")
    expires_at: str = Field(description="授权会话过期时间（UTC）")
    environment: str = Field(description="本次授权使用的应用环境")
    store_code: str = Field(default="", description="ERP 内部店铺编码")
    service_id: str = Field(default="", description="TikTok Shop Service ID / App ID")
    market: str = Field(default="ROW", description="TikTok 授权市场 ROW/US")


class DisconnectOut(BaseModel):
    store_id: str = Field(description="店铺ID")
    status: str = Field(description="断开后的店铺状态")


class ListingOut(BaseModel):
    id: str = Field(description="平台商品行ID")
    store_id: str = Field(description="店铺ID")
    external_item_id: str = Field(description="平台商品ID")
    external_sku_id: str = Field(description="平台规格ID / SKU 编码；无规格为空串")
    title: str = Field(description="平台商品标题")
    price: str | None = Field(default=None, description="平台售价（字符串）")
    currency: str = Field(description="币种")
    remote_stock: int | None = Field(default=None, description="平台可售库存快照")
    status: str = Field(description="平台商品状态")
    synced_at: str | None = Field(default=None, description="最近同步时间（UTC）")
    link_status: str | None = Field(default=None, description="映射状态")
    mapping_source: str | None = Field(default=None, description="映射来源 auto/manual")
    product_id: str | None = Field(default=None, description="本地商品ID")
    sku_id: str | None = Field(default=None, description="本地 SKU ID")
    sku_code: str | None = Field(default=None, description="本地 SKU 编码")
    product_name: str | None = Field(default=None, description="本地商品名称")


class ListingLinkOut(BaseModel):
    listing_id: str = Field(description="平台商品行ID")
    product_id: str | None = Field(default=None, description="本地商品ID")
    sku_id: str | None = Field(default=None, description="本地 SKU ID")
    link_status: str = Field(description="映射状态")
    mapping_source: str = Field(description="映射来源")


# --------------------------------------------------------------------------- 组装
def store_payload(summary: Any) -> dict[str, Any]:
    """``StoreSummary`` → 响应字典。"""
    return {
        "id": id_string(summary.id),
        "channel": summary.channel,
        "store_code": summary.store_code,
        "name": summary.name,
        "external_store_id": summary.external_store_id,
        "region": summary.region,
        "timezone": summary.timezone,
        "status": summary.status,
        "environment": summary.environment,
        "authorized_at": utc_iso(summary.authorized_at),
        "last_synced_at": utc_iso(summary.last_synced_at),
        "disconnected_at": utc_iso(summary.disconnected_at),
        "last_error": summary.last_error,
        "credential_status": summary.credential_status,
        "access_token_expires_at": utc_iso(summary.access_token_expires_at),
        "refresh_token_expires_at": utc_iso(summary.refresh_token_expires_at),
    }


def listing_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Service 返回的平台商品字典 → 响应字典（ID / 金额转字符串）。"""
    return {
        "id": id_string(item.get("id")),
        "store_id": id_string(item.get("store_id")),
        "external_item_id": item.get("external_item_id") or "",
        "external_sku_id": item.get("external_sku_id") or "",
        "title": item.get("title") or "",
        "price": decimal_string(item.get("price")),
        "currency": item.get("currency") or "",
        "remote_stock": item.get("remote_stock"),
        "status": item.get("status") or "",
        "synced_at": utc_iso(item.get("synced_at")),
        "link_status": item.get("link_status"),
        "mapping_source": item.get("mapping_source"),
        "product_id": id_string(item.get("product_id")),
        "sku_id": id_string(item.get("sku_id")),
        "sku_code": item.get("sku_code"),
        "product_name": item.get("product_name"),
    }


def link_payload(item: dict[str, Any]) -> dict[str, Any]:
    """映射结果 → 响应字典。"""
    return {
        "listing_id": id_string(item.get("listing_id")),
        "product_id": id_string(item.get("product_id")),
        "sku_id": id_string(item.get("sku_id")),
        "link_status": item.get("link_status") or "",
        "mapping_source": item.get("mapping_source") or "",
    }


def authorize_payload(item: dict[str, Any]) -> dict[str, Any]:
    """授权会话 → 响应字典。"""
    return {
        "auth_url": item.get("auth_url") or "",
        "expires_at": utc_iso(item.get("expires_at")) or "",
        "environment": item.get("environment") or "",
        "store_code": item.get("store_code") or "",
        "service_id": item.get("service_id") or "",
        "market": item.get("market") or "ROW",
    }


class TikTokAuthorizeIn(ShopeeAuthorizeIn):
    """发起 TikTok Shop 店铺授权（字段与 Shopee 一致：environment/redirect_path）。"""
