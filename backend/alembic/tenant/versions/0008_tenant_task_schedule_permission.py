"""仅为系统所有者和管理员补齐定时任务管理权限。无外键、无新增业务表。

Revision ID: tenant_0008
Revises: tenant_0007
"""
from alembic import op

revision = "tenant_0008"
down_revision = "tenant_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO role_permissions (tenant_id, role_id, permission_code, created_at, updated_at)
        SELECT r.tenant_id, r.id, 'task.schedule.manage', UTC_TIMESTAMP(), UTC_TIMESTAMP()
        FROM roles r
        WHERE r.is_system = 1 AND r.code IN ('owner', 'admin')
        AND NOT EXISTS (
            SELECT 1 FROM role_permissions p
            WHERE p.tenant_id = r.tenant_id AND p.role_id = r.id
            AND p.permission_code = 'task.schedule.manage'
        )
    """)
    op.execute("UPDATE tenant_settings SET permission_version = permission_version + 1")


def downgrade() -> None:
    op.execute("DELETE FROM role_permissions WHERE permission_code = 'task.schedule.manage'")
    op.execute("UPDATE tenant_settings SET permission_version = permission_version + 1")
