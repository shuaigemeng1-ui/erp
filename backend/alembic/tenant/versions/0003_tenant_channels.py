"""租户库迁移：店铺、店铺凭据、平台商品与 SKU 映射。

Revision ID: tenant_0003
Revises: tenant_0002

约定（plan.txt §2「无外键规范」）：

* 只建 ``stores``、``store_credentials``、``channel_listings``、
  ``channel_listing_links`` 四张表，禁止外键、禁止数据库级联删除；
* 平台令牌只存 AES-GCM 密文（``enc:v1:`` 前缀）；
* 平台商品唯一键 ``(tenant_id, store_id, external_item_id, external_sku_id)``，
  无规格商品的 ``external_sku_id`` 使用空串；
* 所有表 / 字段带中文 COMMENT，UTF-8 编码。

实现负责人：backend-channels。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Alembic 版本标识（Lead 固定，勿改）
revision = "tenant_0003"
down_revision = "tenant_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ 店铺
    op.create_table(
        "stores",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
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
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("channel", sa.String(length=32), nullable=False, comment="渠道编码，如 shopee"),
        sa.Column(
            "environment",
            sa.String(length=16),
            server_default=sa.text("'test'"),
            nullable=False,
            comment="授权环境：production/test",
        ),
        sa.Column(
            "name",
            sa.String(length=128),
            server_default=sa.text("''"),
            nullable=False,
            comment="店铺名称",
        ),
        sa.Column(
            "external_store_id",
            sa.String(length=64),
            nullable=False,
            comment="平台店铺ID（Shopee shop_id）",
        ),
        sa.Column("region", sa.String(length=16), nullable=True, comment="站点区域，如 SG/MY/PH"),
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default=sa.text("'UTC'"),
            nullable=False,
            comment="店铺时区（仅展示用，入库时间统一 UTC）",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'connected'"),
            nullable=False,
            comment="状态：connected/reauth_required/disconnected",
        ),
        sa.Column("authorized_at", sa.DateTime(), nullable=True, comment="最近一次授权成功时间（UTC）"),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True, comment="最近一次商品同步时间（UTC）"),
        sa.Column("disconnected_at", sa.DateTime(), nullable=True, comment="断开授权时间（UTC）"),
        sa.Column(
            "last_error", sa.String(length=500), nullable=True, comment="最近一次错误摘要（不含凭据）"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_stores"),
        sa.UniqueConstraint(
            "tenant_id",
            "channel",
            "external_store_id",
            name="uq_stores_tenant_id_channel_external_store_id",
        ),
        comment="店铺表",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("ix_stores_tenant_id", "stores", ["tenant_id"], unique=False)

    # ------------------------------------------------------------------ 店铺凭据
    op.create_table(
        "store_credentials",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
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
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("store_id", sa.BigInteger(), nullable=False, comment="店铺ID（stores.id）"),
        sa.Column(
            "access_token_cipher",
            sa.String(length=2048),
            server_default=sa.text("''"),
            nullable=False,
            comment="访问令牌密文（禁止明文）",
        ),
        sa.Column(
            "refresh_token_cipher",
            sa.String(length=2048),
            server_default=sa.text("''"),
            nullable=False,
            comment="刷新令牌密文（禁止明文）",
        ),
        sa.Column(
            "access_token_expires_at", sa.DateTime(), nullable=True, comment="访问令牌过期时间（UTC）"
        ),
        sa.Column(
            "refresh_token_expires_at", sa.DateTime(), nullable=True, comment="刷新令牌过期时间（UTC）"
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="状态：active/expired/invalid/revoked",
        ),
        sa.Column("refreshed_at", sa.DateTime(), nullable=True, comment="最近一次刷新令牌时间（UTC）"),
        sa.Column(
            "last_error", sa.String(length=500), nullable=True, comment="最近一次刷新错误摘要（不含凭据）"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_store_credentials"),
        sa.UniqueConstraint(
            "tenant_id", "store_id", name="uq_store_credentials_tenant_id_store_id"
        ),
        comment="店铺授权凭据表",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("ix_store_credentials_tenant_id", "store_credentials", ["tenant_id"], unique=False)

    # ------------------------------------------------------------------ 平台商品
    op.create_table(
        "channel_listings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
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
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("store_id", sa.BigInteger(), nullable=False, comment="店铺ID（stores.id）"),
        sa.Column(
            "external_item_id", sa.String(length=64), nullable=False, comment="平台商品ID（Shopee item_id）"
        ),
        sa.Column(
            "external_sku_id",
            sa.String(length=64),
            server_default=sa.text("''"),
            nullable=False,
            comment="平台规格ID或平台 SKU 编码；无规格商品为空串",
        ),
        sa.Column(
            "title",
            sa.String(length=255),
            server_default=sa.text("''"),
            nullable=False,
            comment="平台商品标题",
        ),
        sa.Column("price", sa.Numeric(precision=18, scale=4), nullable=True, comment="平台售价"),
        sa.Column(
            "currency", sa.String(length=8), server_default=sa.text("''"), nullable=False, comment="币种编码"
        ),
        sa.Column("remote_stock", sa.Integer(), nullable=True, comment="平台可售库存快照"),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="平台商品状态：active/inactive",
        ),
        sa.Column("raw", sa.JSON(), nullable=True, comment="平台原始数据（用于追溯与规格匹配）"),
        sa.Column("synced_at", sa.DateTime(), nullable=True, comment="最近一次同步时间（UTC）"),
        sa.PrimaryKeyConstraint("id", name="pk_channel_listings"),
        sa.UniqueConstraint(
            "tenant_id",
            "store_id",
            "external_item_id",
            "external_sku_id",
            name="uq_channel_listings_tenant_store_item_sku",
        ),
        comment="平台商品表",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("ix_channel_listings_tenant_id", "channel_listings", ["tenant_id"], unique=False)
    op.create_index(
        "ix_channel_listings_tenant_id_store_id",
        "channel_listings",
        ["tenant_id", "store_id"],
        unique=False,
    )

    # ------------------------------------------------------------------ 平台商品映射
    op.create_table(
        "channel_listing_links",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
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
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("created_by", sa.BigInteger(), nullable=True, comment="创建人用户ID"),
        sa.Column("updated_by", sa.BigInteger(), nullable=True, comment="更新人用户ID"),
        sa.Column(
            "listing_id", sa.BigInteger(), nullable=False, comment="平台商品ID（channel_listings.id）"
        ),
        sa.Column("product_id", sa.BigInteger(), nullable=True, comment="本地商品ID（products.id）"),
        sa.Column("sku_id", sa.BigInteger(), nullable=True, comment="本地 SKU ID（product_skus.id）"),
        sa.Column(
            "link_status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
            comment="映射状态：mapped/pending/ambiguous/missing",
        ),
        sa.Column(
            "mapping_source",
            sa.String(length=16),
            server_default=sa.text("'auto'"),
            nullable=False,
            comment="映射来源：auto/manual",
        ),
        sa.Column("note", sa.String(length=255), nullable=True, comment="映射说明（自动匹配置信度或人工备注）"),
        sa.PrimaryKeyConstraint("id", name="pk_channel_listing_links"),
        sa.UniqueConstraint(
            "tenant_id", "listing_id", name="uq_channel_listing_links_tenant_id_listing_id"
        ),
        comment="平台商品映射表",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "ix_channel_listing_links_tenant_id", "channel_listing_links", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_channel_listing_links_tenant_id_sku_id",
        "channel_listing_links",
        ["tenant_id", "sku_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_channel_listing_links_tenant_id_sku_id", table_name="channel_listing_links")
    op.drop_index("ix_channel_listing_links_tenant_id", table_name="channel_listing_links")
    op.drop_table("channel_listing_links")
    op.drop_index("ix_channel_listings_tenant_id_store_id", table_name="channel_listings")
    op.drop_index("ix_channel_listings_tenant_id", table_name="channel_listings")
    op.drop_table("channel_listings")
    op.drop_index("ix_store_credentials_tenant_id", table_name="store_credentials")
    op.drop_table("store_credentials")
    op.drop_index("ix_stores_tenant_id", table_name="stores")
    op.drop_table("stores")
