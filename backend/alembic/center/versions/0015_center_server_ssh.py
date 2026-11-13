"""中心库：服务器节点 SSH 连接凭据。

Revision ID: center_0015
Revises: center_0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "center_0015"
down_revision = "center_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "server_nodes",
        sa.Column("ssh_host", sa.String(255), nullable=False, server_default="", comment="SSH主机（空则使用探测IP）"),
    )
    op.add_column(
        "server_nodes",
        sa.Column("ssh_port", sa.Integer(), nullable=False, server_default="22", comment="SSH端口"),
    )
    op.add_column(
        "server_nodes",
        sa.Column("ssh_username_cipher", sa.String(1024), nullable=False, server_default="", comment="SSH用户名密文"),
    )
    op.add_column(
        "server_nodes",
        sa.Column("ssh_password_cipher", sa.String(2048), nullable=False, server_default="", comment="SSH密码密文"),
    )


def downgrade() -> None:
    op.drop_column("server_nodes", "ssh_password_cipher")
    op.drop_column("server_nodes", "ssh_username_cipher")
    op.drop_column("server_nodes", "ssh_port")
    op.drop_column("server_nodes", "ssh_host")
