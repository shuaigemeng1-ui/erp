"""租户库模型：店铺、店铺凭据、平台商品、SKU 映射。

约定（见 plan.txt §2「无外键规范」与 docs/data-model.md）：

* 所有表带 ``tenant_id``，跨租户隔离由独立数据库承担，查询必须显式带租户条件。
* 店铺凭据保存 AES-GCM 密文，接口与审计都不得回显明文 Token。
* 平台商品以 ``(tenant_id, store_id, external_item_id, external_sku_id)`` 唯一，
  无规格商品（平台无 model）的 ``external_sku_id`` 使用空串，避免唯一约束失效。
* 平台商品与本地 SKU 的映射单独成表，``manual`` 映射不会被自动匹配覆盖。
* 禁止外键；表名与字段均带中文注释。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    IdMixin,
    OperatorMixin,
    TenantBase,
    TenantIdMixin,
    TimestampMixin,
    table_options,
)


class Store(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """店铺：租户在某个渠道授权接入的经营店铺。"""

    __tablename__ = "stores"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel",
            "external_store_id",
            name="uq_stores_tenant_id_channel_external_store_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "channel",
            "store_code",
            name="uq_stores_tenant_id_channel_store_code",
        ),
        table_options("店铺表"),
    )

    channel: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="渠道编码，如 shopee"
    )
    environment: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="test",
        server_default=text("'test'"),
        comment="授权环境：production/test",
    )
    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="ERP 显示名称（新建店铺时填写）",
    )
    external_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="平台店铺真实名称（只读，展示用）",
    )
    store_code: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="ERP 内部店铺编码（新建店铺时生成，授权前也可用）",
    )
    external_store_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="平台店铺ID（授权前暂存内部编码）"
    )
    region: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="站点区域，如 SG/MY/PH"
    )
    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="UTC",
        server_default=text("'UTC'"),
        comment="店铺时区（仅展示用，入库时间统一 UTC）",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="connected",
        server_default=text("'connected'"),
        comment="状态：connected/reauth_required/disconnected",
    )
    authorized_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最近一次授权成功时间（UTC）"
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最近一次商品同步时间（UTC）"
    )
    disconnected_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="断开授权时间（UTC）"
    )
    last_error: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="最近一次错误摘要（不含凭据）"
    )
    warehouse_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="TikTok Shop 仓库ID（库存推送使用；Shopee 为空）"
    )
    webhook_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
        comment="是否启用平台 Webhook",
    )
    webhook_last_event_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最近一次 Webhook 事件时间（UTC）"
    )


class StoreCredential(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """店铺授权凭据：access / refresh token 密文与过期时间。"""

    __tablename__ = "store_credentials"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "store_id", name="uq_store_credentials_tenant_id_store_id"
        ),
        table_options("店铺授权凭据表"),
    )

    store_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="店铺ID（stores.id）"
    )
    access_token_cipher: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="访问令牌密文（禁止明文）",
    )
    refresh_token_cipher: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="刷新令牌密文（禁止明文）",
    )
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="访问令牌过期时间（UTC）"
    )
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="刷新令牌过期时间（UTC）"
    )
    shop_cipher_cipher: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="店铺 cipher 密文（TikTok Shop shop_cipher；Shopee 为空）",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        server_default=text("'active'"),
        comment="状态：active/expired/invalid/revoked",
    )
    refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最近一次刷新令牌时间（UTC）"
    )
    last_error: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="最近一次刷新错误摘要（不含凭据）"
    )


class TiktokWebhookSubscription(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """TikTok Shop Webhook 订阅状态（按店铺 + 事件类型）。"""

    __tablename__ = "tiktok_webhook_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "store_id", "event_type",
            name="uq_tiktok_webhook_subscriptions_scope",
        ),
        Index("ix_tiktok_webhook_subscriptions_tenant_store", "tenant_id", "store_id"),
        table_options("TikTok Webhook 订阅表"),
    )

    store_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="店铺ID")
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, comment="TikTok 事件类型")
    callback_url: Mapped[str] = mapped_column(String(500), nullable=False, default="", comment="回调地址")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="enabled", comment="enabled/disabled/error"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近校验时间")
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="最近错误")


class TiktokOrderRefreshQueue(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """Webhook 触发的订单定向刷新队列。"""

    __tablename__ = "tiktok_order_refresh_queue"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "store_id", "external_order_no",
            name="uq_tiktok_order_refresh_queue_scope",
        ),
        Index("ix_tiktok_order_refresh_queue_due", "tenant_id", "status", "next_attempt_at"),
        Index("ix_tiktok_order_refresh_queue_lease", "tenant_id", "status", "lease_expires_at"),
        table_options("TikTok 订单刷新队列表"),
    )

    store_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="店铺ID")
    external_order_no: Mapped[str] = mapped_column(String(64), nullable=False, comment="TikTok 订单ID")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="事件合并代数")
    processed_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已处理代数")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", comment="pending/processing/retry_wait/dead/idle"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="尝试次数")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="下次尝试时间")
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="处理者")
    lease_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="租约令牌（确认完成前校验，防旧 Worker 覆盖新版本）"
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="租约过期时间")
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True, comment="最近错误")
    last_failure_code: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="最近失败分类：detail_missing/order_no_mismatch/normalize_failed/..."
    )


class ChannelListing(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """平台商品（店铺远端 item / model 快照）。"""

    __tablename__ = "channel_listings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "store_id",
            "external_item_id",
            "external_sku_id",
            name="uq_channel_listings_tenant_store_item_sku",
        ),
        Index("ix_channel_listings_tenant_id_store_id", "tenant_id", "store_id"),
        table_options("平台商品表"),
    )

    store_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="店铺ID（stores.id）"
    )
    external_item_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="平台商品ID（Shopee item_id）"
    )
    external_sku_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="平台规格ID或平台 SKU 编码；无规格商品为空串",
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="平台商品标题",
    )
    image_url: Mapped[str] = mapped_column(
        String(1000),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="平台商品主图 URL",
    )
    price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True, comment="平台售价"
    )
    currency: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="币种编码",
    )
    remote_stock: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="平台可售库存快照"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        server_default=text("'active'"),
        comment="平台商品状态：active/inactive",
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="平台原始数据（用于追溯与规格匹配）"
    )
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最近一次同步时间（UTC）"
    )


class ChannelListingLink(IdMixin, TimestampMixin, TenantIdMixin, OperatorMixin, TenantBase):
    """平台商品与本地 SKU 的映射关系（每个平台商品一行）。"""

    __tablename__ = "channel_listing_links"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "listing_id", name="uq_channel_listing_links_tenant_id_listing_id"
        ),
        Index("ix_channel_listing_links_tenant_id_sku_id", "tenant_id", "sku_id"),
        table_options("平台商品映射表"),
    )

    listing_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="平台商品ID（channel_listings.id）"
    )
    product_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="本地商品ID（products.id）"
    )
    sku_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="本地 SKU ID（product_skus.id）"
    )
    link_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
        comment="映射状态：mapped/pending/ambiguous/missing",
    )
    mapping_source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="auto",
        server_default=text("'auto'"),
        comment="映射来源：auto/manual",
    )
    note: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="映射说明（自动匹配置信度或人工备注）"
    )
