"""中心库：租户实例部署模式 / 迁移状态，服务器节点 SSH 主机指纹。

批次2「实例隔离与租户模式边界」：

* ``tenant_instances.deployment_mode``：shared=中心共享，independent=独立部署（已迁出中心）；
* ``tenant_instances.migration_status``：registered/migrating/migrated/rollback；
  迁移中 / 已迁出 / 回滚中的租户不再参与旧中心业务调度，也不能用普通「启用租户」恢复写入；
* ``server_nodes`` 增加 SSH 主机指纹三列，供 SSH 连接前校验主机身份（幂等连接用）。

全部新列可空或带默认值，旧记录读取兼容；无外键，中文注释。

Revision ID: center_0017
Revises: center_0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "center_0017"
down_revision = "center_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ 租户实例：部署模式与迁移状态
    op.add_column(
        "tenant_instances",
        sa.Column(
            "deployment_mode",
            sa.String(16),
            nullable=False,
            server_default="shared",
            comment="部署模式：shared=中心共享，independent=独立部署（已迁出中心）",
        ),
    )
    op.add_column(
        "tenant_instances",
        sa.Column(
            "migration_status",
            sa.String(16),
            nullable=False,
            server_default="registered",
            comment="迁移状态：registered=已登记，migrating=迁移中，migrated=已迁出，rollback=回滚",
        ),
    )
    op.create_index(
        "ix_tenant_instances_deployment_mode", "tenant_instances", ["deployment_mode"], unique=False
    )
    op.create_index(
        "ix_tenant_instances_migration_status", "tenant_instances", ["migration_status"], unique=False
    )

    # ------------------------------------------------------------------ 服务器节点：SSH 主机指纹
    op.add_column(
        "server_nodes",
        sa.Column(
            "ssh_host_fingerprint",
            sa.String(128),
            nullable=False,
            server_default="",
            comment="SSH主机公钥指纹（SHA256:xxxx；用于连接前校验主机身份）",
        ),
    )
    op.add_column(
        "server_nodes",
        sa.Column(
            "ssh_host_fingerprint_verified_at", sa.DateTime(), nullable=True, comment="主机指纹首次确认时间UTC"
        ),
    )
    op.add_column(
        "server_nodes",
        sa.Column(
            "ssh_fingerprint_algorithm",
            sa.String(16),
            nullable=False,
            server_default="SHA256",
            comment="主机指纹算法（当前固定SHA256，预留扩展）",
        ),
    )


def downgrade() -> None:
    op.drop_column("server_nodes", "ssh_fingerprint_algorithm")
    op.drop_column("server_nodes", "ssh_host_fingerprint_verified_at")
    op.drop_column("server_nodes", "ssh_host_fingerprint")

    op.drop_index("ix_tenant_instances_migration_status", table_name="tenant_instances")
    op.drop_index("ix_tenant_instances_deployment_mode", table_name="tenant_instances")
    op.drop_column("tenant_instances", "migration_status")
    op.drop_column("tenant_instances", "deployment_mode")
