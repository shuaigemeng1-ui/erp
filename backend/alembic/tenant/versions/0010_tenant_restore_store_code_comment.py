"""修复旧 MODIFY 丢失的店铺编码中文注释，不使用外键。"""
from alembic import op
import sqlalchemy as sa

revision = "tenant_0010"
down_revision = "tenant_0009"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("stores", "store_code", existing_type=sa.String(32),
                    existing_nullable=False, existing_server_default="",
                    comment="ERP 内部店铺编码")


def downgrade():
    # 注释修复不应在回退时主动删除。
    pass
