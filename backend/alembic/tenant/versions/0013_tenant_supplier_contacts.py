"""供应商选填联系人、联系方式和备注；不创建外键。"""
from alembic import op
import sqlalchemy as sa

revision = "tenant_0013"
down_revision = "tenant_0012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("suppliers", sa.Column("contact_name", sa.String(100), nullable=True, comment="供应商联系人（选填）"))
    op.add_column("suppliers", sa.Column("contact_details", sa.String(255), nullable=True, comment="供应商联系方式（选填）"))
    op.add_column("suppliers", sa.Column("remark", sa.Text(), nullable=True, comment="供应商备注（选填）"))


def downgrade():
    for column in ("remark", "contact_details", "contact_name"):
        op.drop_column("suppliers", column)
