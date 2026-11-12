"""租户本机账号表：独立实例模式下的本地登录基础。

Revision ID: tenant_0014
Revises: tenant_0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "tenant_0014"
down_revision = "tenant_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_local_users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("source_user_id", sa.BigInteger(), nullable=False, comment="来源中心用户ID"),
        sa.Column("email", sa.String(255), nullable=False, comment="登录邮箱"),
        sa.Column("password_hash", sa.String(255), nullable=False, comment="密码哈希"),
        sa.Column("display_name", sa.String(100), nullable=False, server_default="", comment="显示名称"),
        sa.Column("member_no", sa.BigInteger(), nullable=True, comment="租户内成员编号"),
        sa.Column("is_owner", sa.Boolean(), nullable=False, server_default=sa.text("0"), comment="是否负责人"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active", comment="账号状态"),
        sa.Column("synced_at", sa.DateTime(), nullable=False, comment="最近同步UTC"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="创建时间UTC"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="更新时间UTC"),
        sa.UniqueConstraint("tenant_id", "source_user_id", name="uq_tenant_local_users_source"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_tenant_local_users_email"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", comment="租户本机账号表",
    )
    op.create_index("ix_tenant_local_users_tenant_email", "tenant_local_users", ["tenant_id", "email"])


def downgrade() -> None:
    op.drop_table("tenant_local_users")
