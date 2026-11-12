"""租户库模型：订单、明细、状态历史、平台原始数据、订单库存影响。

实现负责人：backend-sales。表与字段必须带中文注释，禁止外键。

设计要点（见 plan.txt §4「商品与订单」「库存与任务可靠性」）：

* ``platform_order_raw`` 先保存平台原始数据，``orders`` / ``order_items`` 再做归一化；
  原始数据表按 (tenant, store, external_order_no) 防重，只保留最新一份，便于重试归一化。
* ``orders`` 以 (tenant_id, store_id, external_order_no) 唯一约束防重：重复拉单 / 回调 / 重试
  只会命中同一条订单，不会重复创建。
* ``order_stock_effects`` 按「订单明细 + 库存地点」记录目标扣减量 ``target_quantity`` 与
  已应用量 ``applied_quantity``，本次差额 = 目标 - 已应用；唯一约束保证同一明细同一地点
  只有一条效果记录，重复执行差额为 0，不会重复扣减或重复恢复。
* 关联字段只用普通 BIGINT ID，不声明外键（plan.txt §2「无外键规范」）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    IdMixin,
    Money,
    TenantBase,
    TenantIdMixin,
    TimestampMixin,
    table_options,
    utcnow,
)


class PlatformOrderRaw(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """平台订单原始数据表：先落库、后归一化，归一化失败可只凭本表重试。"""

    __tablename__ = "platform_order_raw"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "store_id",
            "external_order_no",
            name="uq_platform_order_raw_tenant_store_order",
        ),
        Index(
            "ix_platform_order_raw_tenant_store_order",
            "tenant_id",
            "store_id",
            "external_order_no",
        ),
        table_options("平台订单原始数据表"),
    )

    channel: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="渠道编码（shopee 等）"
    )
    store_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="店铺ID")
    external_order_no: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="平台订单号"
    )
    payload: Mapped[dict] = mapped_column(
        JSON, nullable=False, comment="平台返回的原始订单数据（JSON）"
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="最近一次抓取时间（UTC）"
    )


class OrderSyncCheckpoint(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """每店铺每渠道环境的拉单检查点，不借用其他店铺的任务完成时间。"""

    __tablename__ = "order_sync_checkpoints"
    __table_args__ = (
        UniqueConstraint("tenant_id", "store_id", "channel", "environment",
                         name="uq_order_sync_checkpoint_scope"),
        table_options("店铺订单同步检查点表"),
    )

    store_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="店铺ID")
    channel: Mapped[str] = mapped_column(String(32), nullable=False, comment="渠道编码")
    environment: Mapped[str] = mapped_column(String(16), nullable=False, comment="平台应用环境")
    watermark: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="完整成功窗口的截止时间（UTC）")
    window_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="当前固定查询窗口开始时间（UTC）")
    window_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="当前固定查询窗口截止时间（UTC）")
    cursor: Mapped[str] = mapped_column(String(2048), nullable=False, default="", comment="当前窗口尚未完成的分页游标")


class Order(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """统一订单表：平台订单归一化后的唯一事实，按店铺 + 平台订单号防重。"""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "store_id",
            "external_order_no",
            name="uq_orders_tenant_store_external_no",
        ),
        Index("ix_orders_tenant_status_placed", "tenant_id", "status", "placed_at"),
        Index("ix_orders_tenant_store_placed", "tenant_id", "store_id", "placed_at"),
        table_options("统一订单表"),
    )

    store_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="店铺ID")
    channel: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="渠道编码（shopee 等）"
    )
    external_order_no: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="平台订单号"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        comment="归一化订单状态（pending/paid/shipped/completed/cancelled/unknown）",
    )
    platform_status: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="平台原始订单状态"
    )
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="", comment="币种（ISO 4217）"
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=0, comment="订单总额（平台币种，DECIMAL）"
    )
    buyer_name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", comment="买家名称"
    )
    placed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="平台下单时间（UTC）"
    )
    raw_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="对应平台原始数据ID"
    )
    abnormal_reason: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        default="",
        comment="异常原因（未映射SKU/未知状态/负库存等，非空表示需人工处理）",
    )


class OrderItem(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """订单明细表：平台明细行归一化结果；未映射 SKU 时 sku_id 为空并标记映射状态。"""

    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "order_id",
            "external_item_id",
            "external_sku_id",
            name="uq_order_items_tenant_order_external",
        ),
        Index("ix_order_items_tenant_order", "tenant_id", "order_id"),
        Index("ix_order_items_tenant_order_platform_sku", "tenant_id", "order_id", "external_item_id", "platform_sku_id"),
        table_options("订单明细表"),
    )

    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="订单ID")
    external_item_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="平台商品ID"
    )
    external_sku_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", comment="平台规格/卖家SKU编码"
    )
    platform_sku_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="平台原生SKU ID（用于订单明细稳定识别）"
    )
    sku_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="本地SKU ID（未映射时为空）"
    )
    item_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", comment="商品名称快照"
    )
    quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="购买数量"
    )
    unit_price: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=0, comment="成交单价（DECIMAL）"
    )
    discount_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=0, comment="明细优惠金额（DECIMAL）"
    )
    item_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", comment="平台明细状态"
    )
    mapping_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="missing",
        comment="SKU 映射状态（mapped/missing/ambiguous）",
    )


class OrderStatusHistory(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """订单状态历史表：只在状态真正变化时追加一行，来源可追溯。"""

    __tablename__ = "order_status_history"
    __table_args__ = (
        Index(
            "ix_order_status_history_tenant_order",
            "tenant_id",
            "order_id",
            "changed_at",
        ),
        table_options("订单状态历史表"),
    )

    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="订单ID")
    from_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", comment="变更前归一化状态（新建订单为空）"
    )
    to_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", comment="变更后归一化状态"
    )
    platform_status: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="变更时平台原始状态"
    )
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="sync",
        comment="变更来源（sync/webhook/manual/system）",
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="变更时间（UTC）"
    )


class OrderStockEffect(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """订单库存影响表：记录每条明细在每个库存地点应扣/已扣数量，用于差额幂等。"""

    __tablename__ = "order_stock_effects"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "order_item_id",
            "stock_place_id",
            name="uq_order_stock_effects_tenant_item_place",
        ),
        Index(
            "ix_order_stock_effects_tenant_order", "tenant_id", "order_id"
        ),
        Index(
            "ix_order_stock_effects_tenant_status", "tenant_id", "status"
        ),
        table_options("订单库存影响表"),
    )

    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="订单ID")
    order_item_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="订单明细ID"
    )
    sku_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="本地SKU ID")
    stock_place_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="库存地点ID"
    )
    applied_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已应用的库存影响数量（正数=已扣减）"
    )
    target_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="目标库存影响数量（正数=应扣减）"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        comment="影响状态（pending/applied/skipped/failed）",
    )
    last_task_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="最近一次处理的任务标识"
    )
