"""订单同步检查点按店铺、渠道、环境隔离；不使用外键。"""

import sqlalchemy as sa
from alembic import op

revision = "tenant_0009"
down_revision = "tenant_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_sync_checkpoints",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True, comment="主键ID"),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False, comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False, comment="更新时间（UTC）"),
        sa.Column("store_id", sa.BigInteger(), nullable=False, comment="店铺ID"),
        sa.Column("channel", sa.String(32), nullable=False, comment="渠道编码"),
        sa.Column("environment", sa.String(16), nullable=False, comment="平台应用环境"),
        sa.Column("watermark", sa.DateTime(), nullable=True, comment="完整成功窗口的截止时间（UTC）"),
        sa.Column("window_start", sa.DateTime(), nullable=True, comment="当前固定查询窗口开始时间（UTC）"),
        sa.Column("window_end", sa.DateTime(), nullable=True, comment="当前固定查询窗口截止时间（UTC）"),
        sa.Column("cursor", sa.String(2048), nullable=False, server_default="", comment="当前窗口尚未完成的分页游标"),
        sa.UniqueConstraint("tenant_id", "store_id", "channel", "environment", name="uq_order_sync_checkpoint_scope"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", comment="店铺订单同步检查点表",
    )


def downgrade() -> None:
    op.drop_table("order_sync_checkpoints")
