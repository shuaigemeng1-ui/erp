"""中心库：节点指标 5 分钟/小时聚合。

Revision ID: center_0014
Revises: center_0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "center_0014"
down_revision = "center_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_metric_aggregates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("node_id", sa.BigInteger(), nullable=False, comment="节点ID"),
        sa.Column("resolution", sa.String(8), nullable=False, comment="聚合粒度5m/1h"),
        sa.Column("bucket_start", sa.DateTime(), nullable=False, comment="桶开始UTC"),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0", comment="样本数"),
        sa.Column("cpu_avg", sa.Numeric(6, 2), nullable=False, server_default="0", comment="CPU平均%"),
        sa.Column("cpu_max", sa.Numeric(6, 2), nullable=False, server_default="0", comment="CPU最大%"),
        sa.Column("memory_avg", sa.Numeric(6, 2), nullable=False, server_default="0", comment="内存平均%"),
        sa.Column("memory_max", sa.Numeric(6, 2), nullable=False, server_default="0", comment="内存最大%"),
        sa.Column("load_avg", sa.Numeric(8, 2), nullable=False, server_default="0", comment="负载平均"),
        sa.Column("disk_max", sa.Numeric(6, 2), nullable=False, server_default="0", comment="磁盘最高%"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建UTC"),
        sa.UniqueConstraint("node_id", "resolution", "bucket_start", name="uq_node_metric_agg_bucket"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", comment="节点指标聚合表",
    )
    op.create_index(
        "ix_node_metric_agg_node_res_start",
        "node_metric_aggregates",
        ["node_id", "resolution", "bucket_start"],
    )


def downgrade() -> None:
    op.drop_table("node_metric_aggregates")
