"""每个租户独立的成员编号及不复用编号的高水位；不使用外键。"""
from alembic import op
import sqlalchemy as sa

revision = "center_0009"
down_revision = "center_0008"
branch_labels = None
depends_on = None

MEMBER_COMMENT = "租户内成员编号（每个租户从1递增，内部关联仍使用全局主键ID）"


def upgrade():
    op.add_column("tenants", sa.Column(
        "next_member_no", sa.BigInteger(), nullable=False, server_default="1",
        comment="下一个租户内成员编号（从1开始，已分配编号不重复使用）",
    ))
    op.add_column("tenant_memberships", sa.Column(
        "member_no", sa.BigInteger(), nullable=True, comment=MEMBER_COMMENT,
    ))
    # 窗口派生表在 MySQL 中物化，避免更新表同时作为直接查询源的1093错误。
    # 按首次创建顺序编号，不按当前负责人或激活时间重新排列历史成员。
    op.execute(sa.text("""
        UPDATE tenant_memberships AS target
        JOIN (
            SELECT id, assigned_member_no FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY tenant_id ORDER BY created_at, id
                ) AS assigned_member_no
                FROM tenant_memberships
            ) AS ranked_members
        ) AS numbered ON numbered.id = target.id
        SET target.member_no = numbered.assigned_member_no
    """))
    op.execute(sa.text("""
        UPDATE tenants AS target
        LEFT JOIN (
            SELECT tenant_id, MAX(member_no) + 1 AS next_no
            FROM tenant_memberships GROUP BY tenant_id
        ) AS numbered ON numbered.tenant_id = target.id
        SET target.next_member_no = COALESCE(numbered.next_no, 1)
    """))
    op.alter_column("tenant_memberships", "member_no", existing_type=sa.BigInteger(),
                    existing_nullable=True, nullable=False, comment=MEMBER_COMMENT)
    op.create_unique_constraint("uq_tenant_memberships_tenant_member_no", "tenant_memberships",
                                ["tenant_id", "member_no"])


def downgrade():
    op.drop_constraint("uq_tenant_memberships_tenant_member_no", "tenant_memberships", type_="unique")
    op.drop_column("tenant_memberships", "member_no")
    op.drop_column("tenants", "next_member_no")
