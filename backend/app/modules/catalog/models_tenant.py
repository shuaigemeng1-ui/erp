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

from sqlalchemy import BigInteger, Boolean, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects import mysql
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
    name_en: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="商品英文名称")
    preferred_supplier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="首选供应商ID")
    purchaser_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="采购员租户成员ID")
    developer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="业务开发员租户成员ID")
    inquiry_buyer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="采购询价员租户成员ID")
    hs_code: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="海关编码")
    material_zh: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="中文材质")
    material_en: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="英文材质")
    usage: Mapped[str | None] = mapped_column(String(1000), nullable=True, comment="商品用途")
    feature_tags: Mapped[list[str] | None] = mapped_column(Payload, nullable=True, comment="商品特性标签列表")
    packaging_name: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="包材名称")
    length_cm: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True, comment="商品长度（厘米）")
    width_cm: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True, comment="商品宽度（厘米）")
    height_cm: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True, comment="商品高度（厘米）")
    package_length_cm: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True, comment="包装后长度（厘米）")
    package_width_cm: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True, comment="包装后宽度（厘米）")
    package_height_cm: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True, comment="包装后高度（厘米）")
    sales_type: Mapped[str] = mapped_column(String(16), nullable=False, default="normal", server_default="normal", comment="销售类型：普通、变参、捆绑、组装")
    remark: Mapped[str | None] = mapped_column(Text, nullable=True, comment="商品备注")
    features: Mapped[str | None] = mapped_column(Text, nullable=True, comment="产品特点")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"), comment="商品编辑版本号")


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
    image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True, comment="SKU 独立图片地址")
    guidance_cost: Mapped[Decimal | None] = mapped_column(Money, nullable=True, comment="SKU 指导成本")
    average_cost: Mapped[Decimal | None] = mapped_column(Money, nullable=True, comment="SKU 平均成本（本轮人工维护）")
    weight_g: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True, comment="SKU 重量（克）")
    sales_status: Mapped[str] = mapped_column(String(16), nullable=False, default="on_sale", server_default="on_sale", comment="SKU 销售状态：在售、停售、试卖、清仓")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"), comment="SKU 编辑版本号")


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
    url: Mapped[str] = mapped_column(String(2048), nullable=False, comment="图片地址")
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="排序序号（升序展示）",
    )


class SkuIdentifier(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """SKU 主编号和别名共享的租户内唯一标识空间。"""

    __tablename__ = "sku_identifiers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "normalized_identifier", name="uq_sku_identifiers_tenant_normalized"),
        Index("ix_sku_identifiers_tenant_sku", "tenant_id", "sku_id"),
        table_options("SKU编号与别名表"),
    )
    sku_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="所属SKU ID")
    identifier: Mapped[str] = mapped_column(String(128), nullable=False, comment="SKU编号或别名原文")
    normalized_identifier: Mapped[str] = mapped_column(
        String(384).with_variant(mysql.VARCHAR(384, collation="utf8mb4_bin"), "mysql"),
        nullable=False, comment="去首尾空白并统一大小写后的标识",
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, comment="标识类型：主编号或别名")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"), comment="是否启用该标识")


class Supplier(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """供应商简档，仅用于商品首选供应商维护。"""

    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_suppliers_tenant_code"),
        table_options("供应商简档表"),
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, comment="供应商编码（租户内唯一）")
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="供应商名称")
    contact_name: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="供应商联系人（选填）")
    contact_details: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="供应商联系方式（选填）")
    remark: Mapped[str | None] = mapped_column(Text, nullable=True, comment="供应商备注（选填）")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active", comment="供应商状态：启用或停用")


class CatalogOption(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """租户商品字典：品牌、分类、标签和包材。"""

    __tablename__ = "catalog_options"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "name", name="uq_catalog_options_tenant_kind_name"),
        table_options("商品基础字典表"),
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, comment="字典种类：品牌、分类、标签、包材")
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="字典选项名称")
    parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="父级分类选项ID")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active", comment="选项状态：启用或停用")


__all__ = ["Product", "ProductSku", "ProductImage", "SkuIdentifier", "Supplier", "CatalogOption"]
