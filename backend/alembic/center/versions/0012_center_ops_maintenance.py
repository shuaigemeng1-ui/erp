"""中心库：实例维护窗口。

Revision ID: center_0012
Revises: center_0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "center_0012"
down_revision = "center_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_windows",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="创建时间UTC"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="更新时间UTC"),
        sa.Column("instance_id", sa.BigInteger(), nullable=False, comment="实例ID"),
        sa.Column("starts_at", sa.DateTime(), nullable=False, comment="开始时间UTC"),
        sa.Column("ends_at", sa.DateTime(), nullable=False, comment="结束时间UTC"),
        sa.Column("reason", sa.String(255), nullable=False, server_default="", comment="维护原因"),
        sa.Column("created_by", sa.BigInteger(), nullable=True, comment="创建人用户ID"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", comment="实例维护窗口表",
    )
    op.create_index(
        "ix_maintenance_windows_instance_time",
        "maintenance_windows",
        ["instance_id", "starts_at", "ends_at"],
    )


def downgrade() -> None:
    op.drop_table("maintenance_windows")
