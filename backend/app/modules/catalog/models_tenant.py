"""租户库模型：主商品、SKU、商品图片。

设计要点（见 docs/data-model.md、plan.txt §2「无外键规范」）：

* 禁止外键：``product_id`` / ``sku_id`` 只是普通 BIGINT 关联列，归属与状态由 Service 校验。
* 主商品与可售 SKU 分开；无规格商品同样创建一个 SKU，库存统一按 SKU 维护，避免两套库存写法。
* 成本字段（``cost_amount`` / ``cost_currency``）受 ``catalog.cost.read`` 权限控制：
  无权限时查询不读取、响应不输出。
* 删除优先归档：``status`` 使用 ``active`` / ``archived``，不物理删除。
* 金额统一 ``DECIMAL(18,4)`` + Python ``Decimal``；时间统一 UTC。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    IdMixin,
    Money,
    Payload,
    TenantBase,
    TenantIdMixin,
    TimestampMixin,
    table_options,
)


class Product(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """主商品表：商品维度的基础资料，不直接持有库存。"""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_products_tenant_id_code"),
        table_options("商品表"),
    )

    code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="商品编码（租户内唯一）"
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="商品名称")
    brand: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="品牌")
    category: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="商品分类")
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        server_default="active",
        comment="商品状态：active 启用 / archived 已归档",
    )
    cost_amount: Mapped[Decimal | None] = mapped_column(
        Money, nullable=True, comment="成本金额（需 catalog.cost.read 权限）"
    )
    cost_currency: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="CNY",
        server_default="CNY",
        comment="成本币种",
    )
    base_price: Mapped[Decimal] = mapped_column(
        Money,
        nullable=False,
        default=Decimal("0"),
        server_default=text("0.0000"),
        comment="基础售价",
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="商品描述")


class ProductSku(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """可售 SKU 表：库存、映射与订单扣减都以 SKU 为最小单位。"""

    __tablename__ = "product_skus"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sku_code", name="uq_product_skus_tenant_id_sku_code"),
        Index("ix_product_skus_product_id", "product_id"),
        table_options("商品SKU表"),
    )

    product_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="商品ID（关联 products.id，无外键）"
    )
    sku_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="SKU 编码（租户内唯一）"
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="SKU 名称")
    specs: Mapped[dict[str, Any] | None] = mapped_column(
        Payload, nullable=True, comment="规格信息（JSON，如颜色/尺码）"
    )
    price: Mapped[Decimal] = mapped_column(
        Money,
        nullable=False,
        default=Decimal("0"),
        server_default=text("0.0000"),
        comment="SKU 售价",
    )
    cost_amount: Mapped[Decimal | None] = mapped_column(
        Money, nullable=True, comment="SKU 成本金额（需 catalog.cost.read 权限）"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        server_default="active",
        comment="SKU 状态：active 启用 / archived 已归档",
    )


class ProductImage(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """商品图片表：``sku_id`` 为空表示商品级主图，否则为 SKU 图。"""

    __tablename__ = "product_images"
    __table_args__ = (
        Index("ix_product_images_product_id", "product_id"),
        Index("ix_product_images_sku_id", "sku_id"),
        table_options("商品图片表"),
    )

    product_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="商品ID（关联 products.id，无外键）"
    )
    sku_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="SKU ID（关联 product_skus.id，无外键；为空表示商品主图）"
    )
    url: Mapped[str] = mapped_column(String(500), nullable=False, comment="图片地址")
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="排序序号（升序展示）",
    )


__all__ = ["Product", "ProductSku", "ProductImage"]
