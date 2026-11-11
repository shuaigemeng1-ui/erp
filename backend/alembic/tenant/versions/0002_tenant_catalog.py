"""租户库：商品、SKU、图片、库存地点、库存余额、库存流水。

Revision ID: tenant_0002
Revises: tenant_0001

规范（plan.txt §2「无外键规范」）：

* 不创建任何外键 / 级联删除，关联字段只是普通 BIGINT 列 + 索引；
* 每张表、每个字段都有中文注释；
* InnoDB + utf8mb4，金额 DECIMAL(18,4)，时间存 UTC。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "tenant_0002"
down_revision = "tenant_0001"
branch_labels = None
depends_on = None

_TABLE_KW: dict[str, str] = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def _timestamps() -> list[sa.Column]:
    """创建 / 更新时间列（UTC，数据库默认当前时间）。"""
    return [
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
    ]


def upgrade() -> None:  # noqa: C901 - 迁移文件按表顺序罗列，便于人工核对
    # ------------------------------------------------------------------ 商品
    op.create_table(
        "products",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("code", sa.String(length=64), nullable=False, comment="商品编码（租户内唯一）"),
        sa.Column("name", sa.String(length=200), nullable=False, comment="商品名称"),
        sa.Column("brand", sa.String(length=100), nullable=True, comment="品牌"),
        sa.Column("category", sa.String(length=100), nullable=True, comment="商品分类"),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="商品状态：active 启用 / archived 已归档",
        ),
        sa.Column("cost_amount", sa.Numeric(18, 4), nullable=True, comment="成本金额（需 catalog.cost.read 权限）"),
        sa.Column(
            "cost_currency",
            sa.String(length=8),
            server_default=sa.text("'CNY'"),
            nullable=False,
            comment="成本币种",
        ),
        sa.Column(
            "base_price",
            sa.Numeric(18, 4),
            server_default=sa.text("0.0000"),
            nullable=False,
            comment="基础售价",
        ),
        sa.Column("description", sa.Text(), nullable=True, comment="商品描述"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_products"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_products_tenant_id_code"),
        comment="商品表",
        **_TABLE_KW,
    )
    op.create_index("ix_products_tenant_id", "products", ["tenant_id"], unique=False)

    op.create_table(
        "product_skus",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("product_id", sa.BigInteger(), nullable=False, comment="商品ID（关联 products.id，无外键）"),
        sa.Column("sku_code", sa.String(length=64), nullable=False, comment="SKU 编码（租户内唯一）"),
        sa.Column("name", sa.String(length=200), nullable=False, comment="SKU 名称"),
        sa.Column("specs", sa.JSON(), nullable=True, comment="规格信息（JSON，如颜色/尺码）"),
        sa.Column(
            "price",
            sa.Numeric(18, 4),
            server_default=sa.text("0.0000"),
            nullable=False,
            comment="SKU 售价",
        ),
        sa.Column("cost_amount", sa.Numeric(18, 4), nullable=True, comment="SKU 成本金额（需 catalog.cost.read 权限）"),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="SKU 状态：active 启用 / archived 已归档",
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_product_skus"),
        sa.UniqueConstraint("tenant_id", "sku_code", name="uq_product_skus_tenant_id_sku_code"),
        comment="商品SKU表",
        **_TABLE_KW,
    )
    op.create_index("ix_product_skus_tenant_id", "product_skus", ["tenant_id"], unique=False)
    op.create_index("ix_product_skus_product_id", "product_skus", ["product_id"], unique=False)

    op.create_table(
        "product_images",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("product_id", sa.BigInteger(), nullable=False, comment="商品ID（关联 products.id，无外键）"),
        sa.Column(
            "sku_id",
            sa.BigInteger(),
            nullable=True,
            comment="SKU ID（关联 product_skus.id，无外键；为空表示商品主图）",
        ),
        sa.Column("url", sa.String(length=500), nullable=False, comment="图片地址"),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="排序序号（升序展示）",
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_product_images"),
        comment="商品图片表",
        **_TABLE_KW,
    )
    op.create_index("ix_product_images_tenant_id", "product_images", ["tenant_id"], unique=False)
    op.create_index("ix_product_images_product_id", "product_images", ["product_id"], unique=False)
    op.create_index("ix_product_images_sku_id", "product_images", ["sku_id"], unique=False)

    # ------------------------------------------------------------------ 库存
    op.create_table(
        "stock_places",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("code", sa.String(length=64), nullable=False, comment="库存地点编码（租户内唯一）"),
        sa.Column("name", sa.String(length=100), nullable=False, comment="库存地点名称"),
        sa.Column(
            "is_default",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
            comment="是否默认库存地点（每租户开通时创建一个）",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="状态：active 启用 / archived 已归档",
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_stock_places"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_stock_places_tenant_id_code"),
        comment="库存地点表",
        **_TABLE_KW,
    )
    op.create_index("ix_stock_places_tenant_id", "stock_places", ["tenant_id"], unique=False)

    op.create_table(
        "inventory_balances",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("sku_id", sa.BigInteger(), nullable=False, comment="SKU ID（关联 product_skus.id，无外键）"),
        sa.Column(
            "stock_place_id",
            sa.BigInteger(),
            nullable=False,
            comment="库存地点ID（关联 stock_places.id，无外键）",
        ),
        sa.Column(
            "available_quantity",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="可用库存数量（外部订单允许为负，人工调整不允许）",
        ),
        sa.Column(
            "reserved_quantity",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="占用库存数量",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="版本号（每次数量变化 +1，用于并发与审计）",
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_inventory_balances"),
        sa.UniqueConstraint(
            "tenant_id",
            "sku_id",
            "stock_place_id",
            name="uq_inventory_balances_tenant_id_sku_id_stock_place_id",
        ),
        comment="库存余额表",
        **_TABLE_KW,
    )
    op.create_index(
        "ix_inventory_balances_tenant_id", "inventory_balances", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_inventory_balances_stock_place_id",
        "inventory_balances",
        ["stock_place_id"],
        unique=False,
    )

    op.create_table(
        "stock_movements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("sku_id", sa.BigInteger(), nullable=False, comment="SKU ID（关联 product_skus.id，无外键）"),
        sa.Column(
            "stock_place_id",
            sa.BigInteger(),
            nullable=False,
            comment="库存地点ID（关联 stock_places.id，无外键）",
        ),
        sa.Column("quantity_change", sa.Integer(), nullable=False, comment="变动数量（正数入库，负数出库）"),
        sa.Column("balance_after", sa.Integer(), nullable=False, comment="变动后的可用库存数量"),
        sa.Column(
            "movement_type",
            sa.String(length=32),
            nullable=False,
            comment="流水类型：manual_adjust/order_deduct/order_restore 等",
        ),
        sa.Column(
            "reference_type",
            sa.String(length=32),
            server_default=sa.text("''"),
            nullable=False,
            comment="关联业务类型（如 order，无关联时为空）",
        ),
        sa.Column("reference_id", sa.BigInteger(), nullable=True, comment="关联业务ID（无外键）"),
        sa.Column("operator_id", sa.BigInteger(), nullable=True, comment="操作人用户ID（自动任务为空）"),
        sa.Column(
            "note",
            sa.String(length=500),
            server_default=sa.text("''"),
            nullable=False,
            comment="备注",
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_stock_movements"),
        comment="库存流水表",
        **_TABLE_KW,
    )
    op.create_index("ix_stock_movements_tenant_id", "stock_movements", ["tenant_id"], unique=False)
    op.create_index(
        "ix_stock_movements_stock_place_id",
        "stock_movements",
        ["stock_place_id"],
        unique=False,
    )
    op.create_index(
        "ix_stock_movements_tenant_id_sku_id_created_at",
        "stock_movements",
        ["tenant_id", "sku_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_stock_movements_tenant_id_reference_type_reference_id",
        "stock_movements",
        ["tenant_id", "reference_type", "reference_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_stock_movements_tenant_id_reference_type_reference_id",
        table_name="stock_movements",
    )
    op.drop_index("ix_stock_movements_tenant_id_sku_id_created_at", table_name="stock_movements")
    op.drop_index("ix_stock_movements_stock_place_id", table_name="stock_movements")
    op.drop_index("ix_stock_movements_tenant_id", table_name="stock_movements")
    op.drop_table("stock_movements")

    op.drop_index("ix_inventory_balances_stock_place_id", table_name="inventory_balances")
    op.drop_index("ix_inventory_balances_tenant_id", table_name="inventory_balances")
    op.drop_table("inventory_balances")

    op.drop_index("ix_stock_places_tenant_id", table_name="stock_places")
    op.drop_table("stock_places")

    op.drop_index("ix_product_images_sku_id", table_name="product_images")
    op.drop_index("ix_product_images_product_id", table_name="product_images")
    op.drop_index("ix_product_images_tenant_id", table_name="product_images")
    op.drop_table("product_images")

    op.drop_index("ix_product_skus_product_id", table_name="product_skus")
    op.drop_index("ix_product_skus_tenant_id", table_name="product_skus")
    op.drop_table("product_skus")

    op.drop_index("ix_products_tenant_id", table_name="products")
    op.drop_table("products")
