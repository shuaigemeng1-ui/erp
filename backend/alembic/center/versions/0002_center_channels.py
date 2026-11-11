"""中心库迁移：平台渠道应用与外部店铺归属登记。

Revision ID: center_0002
Revises: center_0001

约定（plan.txt §2「无外键规范」）：

* 只建 ``platform_channel_apps``、``channel_account_registry`` 两张表，禁止外键；
* 平台应用密钥以密文存储（``enc:v1:`` 前缀），本迁移只建列，不写数据；
* 同一环境同一外部店铺只允许一个有效租户，由唯一约束保证；
* 所有表 / 字段带中文 COMMENT，UTF-8 编码。

实现负责人：backend-channels。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Alembic 版本标识（Lead 固定，勿改）
revision = "center_0002"
down_revision = "center_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ 平台渠道应用
    op.create_table(
        "platform_channel_apps",
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
        sa.Column("channel", sa.String(length=32), nullable=False, comment="渠道编码，如 shopee"),
        sa.Column(
            "environment", sa.String(length=16), nullable=False, comment="应用环境：production/test"
        ),
        sa.Column("partner_id", sa.BigInteger(), nullable=True, comment="平台合作方ID（Partner ID）"),
        sa.Column(
            "partner_key_cipher",
            sa.String(length=1024),
            nullable=True,
            comment="合作方密钥密文（用于请求签名，禁止明文）",
        ),
        sa.Column(
            "partner_secret_cipher",
            sa.String(length=1024),
            nullable=True,
            comment="推送校验密钥密文，为空时回退使用合作方密钥",
        ),
        sa.Column(
            "redirect_url", sa.String(length=512), nullable=True, comment="OAuth 回调地址（平台统一配置）"
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="状态：active/disabled",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_platform_channel_apps"),
        sa.UniqueConstraint(
            "channel", "environment", name="uq_platform_channel_apps_channel_environment"
        ),
        comment="平台渠道应用配置表",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )

    # ------------------------------------------------------------------ 外部店铺归属
    op.create_table(
        "channel_account_registry",
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
        sa.Column("channel", sa.String(length=32), nullable=False, comment="渠道编码，如 shopee"),
        sa.Column(
            "environment", sa.String(length=16), nullable=False, comment="环境：production/test"
        ),
        sa.Column(
            "external_account_id",
            sa.String(length=64),
            nullable=False,
            comment="平台外部账号ID（Shopee 店铺ID）",
        ),
        sa.Column("tenant_id", sa.BigInteger(), nullable=True, comment="归属租户ID"),
        sa.Column("store_id", sa.BigInteger(), nullable=True, comment="归属租户库内的店铺ID"),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'bound'"),
            nullable=False,
            comment="状态：bound/released",
        ),
        sa.Column("bound_at", sa.DateTime(), nullable=True, comment="绑定时间（UTC）"),
        sa.Column("released_at", sa.DateTime(), nullable=True, comment="释放时间（UTC）"),
        sa.PrimaryKeyConstraint("id", name="pk_channel_account_registry"),
        sa.UniqueConstraint(
            "channel",
            "environment",
            "external_account_id",
            name="uq_channel_account_registry_env_external",
        ),
        comment="渠道外部账号归属登记表",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "ix_channel_account_registry_channel_external_account_id",
        "channel_account_registry",
        ["channel", "external_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_channel_account_registry_tenant_id",
        "channel_account_registry",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_channel_account_registry_tenant_id", table_name="channel_account_registry")
    op.drop_index(
        "ix_channel_account_registry_channel_external_account_id",
        table_name="channel_account_registry",
    )
    op.drop_table("channel_account_registry")
    op.drop_table("platform_channel_apps")
