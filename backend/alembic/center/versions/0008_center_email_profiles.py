"""中心库：平台邮件服务账号（阿里云 DirectMail / 腾讯云 SES）。

Revision ID: center_0008
Revises: center_0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "center_0008"
down_revision = "center_0007"
branch_labels = None
depends_on = None


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="更新时间（UTC）"),
    ]


def upgrade() -> None:
    op.create_table(
        "email_profiles",
        *_base(),
        sa.Column("name", sa.String(length=100), nullable=False, comment="邮件账号名称"),
        sa.Column("provider", sa.String(length=32), nullable=False, comment="邮件厂商：aliyun_directmail/tencent_ses"),
        sa.Column("region", sa.String(length=64), nullable=False, server_default="", comment="服务区域"),
        sa.Column("from_address", sa.String(length=255), nullable=False, comment="发信地址"),
        sa.Column("from_name", sa.String(length=100), nullable=False, server_default="", comment="发信人名称"),
        sa.Column("access_key_id_cipher", sa.Text(), nullable=False, comment="AccessKey ID 密文"),
        sa.Column("secret_access_key_cipher", sa.Text(), nullable=False, comment="AccessKey Secret / SecretKey 密文"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1"), comment="是否启用"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0"), comment="是否默认发信账号"),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1"), comment="配置版本号"),
        sa.UniqueConstraint("name", name="uq_email_profiles_name"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        comment="平台邮件服务账号配置表",
    )


def downgrade() -> None:
    op.drop_table("email_profiles")
