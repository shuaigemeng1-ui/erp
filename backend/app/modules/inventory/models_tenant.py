"""租户库模型：库存地点、库存余额、库存流水。

设计要点（见 docs/data-model.md、plan.txt §4「库存与任务可靠性」）：

* 禁止外键：``sku_id`` / ``stock_place_id`` 只是普通 BIGINT 关联列，由 Service 校验归属。
* 一张 ``inventory_balances`` 行表示一个 SKU 在一个库存地点的余额；
  ``version`` 用于条件更新 / 乐观锁审计，配合 ``SELECT ... FOR UPDATE`` 防并发丢数量。
* ``stock_movements`` 是库存变化的唯一事实来源，余额与流水必须在同一事务内写入。
* 人工调整禁止负库存；外部已成立订单允许负库存（``allow_negative=True``），由调用方标记异常。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    IdMixin,
    Quantity,
    TenantBase,
    TenantIdMixin,
    TimestampMixin,
    table_options,
)


class StockPlace(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """库存地点表：默认地点在租户开通时由 identity 模块创建。"""

    __tablename__ = "stock_places"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_stock_places_tenant_id_code"),
        table_options("库存地点表"),
    )

    code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="库存地点编码（租户内唯一）"
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="库存地点名称")
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
        comment="是否默认库存地点（每租户开通时创建一个）",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        server_default="active",
        comment="状态：active 启用 / archived 已归档",
    )


class InventoryBalance(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """库存余额表：SKU × 库存地点唯一，可用数量与占用数量分开维护。"""

    __tablename__ = "inventory_balances"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "sku_id",
            "stock_place_id",
            name="uq_inventory_balances_tenant_id_sku_id_stock_place_id",
        ),
        Index("ix_inventory_balances_stock_place_id", "stock_place_id"),
        table_options("库存余额表"),
    )

    sku_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="SKU ID（关联 product_skus.id，无外键）"
    )
    stock_place_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="库存地点ID（关联 stock_places.id，无外键）"
    )
    available_quantity: Mapped[int] = mapped_column(
        Quantity,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="可用库存数量（外部订单允许为负，人工调整不允许）",
    )
    reserved_quantity: Mapped[int] = mapped_column(
        Quantity,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="占用库存数量",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="版本号（每次数量变化 +1，用于并发与审计）",
    )


class StockMovement(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """库存流水表：记录每一次数量变化及变化后的余额。"""

    __tablename__ = "stock_movements"
    __table_args__ = (
        Index(
            "ix_stock_movements_tenant_id_sku_id_created_at",
            "tenant_id",
            "sku_id",
            "created_at",
        ),
        Index("ix_stock_movements_stock_place_id", "stock_place_id"),
        Index(
            "ix_stock_movements_tenant_id_reference_type_reference_id",
            "tenant_id",
            "reference_type",
            "reference_id",
        ),
        table_options("库存流水表"),
    )

    sku_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="SKU ID（关联 product_skus.id，无外键）"
    )
    stock_place_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="库存地点ID（关联 stock_places.id，无外键）"
    )
    quantity_change: Mapped[int] = mapped_column(
        Quantity, nullable=False, comment="变动数量（正数入库，负数出库）"
    )
    balance_after: Mapped[int] = mapped_column(
        Quantity, nullable=False, comment="变动后的可用库存数量"
    )
    movement_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="流水类型：manual_adjust/order_deduct/order_restore 等"
    )
    reference_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="",
        server_default="",
        comment="关联业务类型（如 order，无关联时为空）",
    )
    reference_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="关联业务ID（无外键）"
    )
    operator_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="操作人用户ID（自动任务为空）"
    )
    note: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        default="",
        server_default="",
        comment="备注",
    )


__all__ = ["StockPlace", "InventoryBalance", "StockMovement"]
