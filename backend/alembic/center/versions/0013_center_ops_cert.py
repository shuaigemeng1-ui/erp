"""中心库：探测记录增加 HTTPS 证书剩余天数。

Revision ID: center_0013
Revises: center_0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "center_0013"
down_revision = "center_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "node_checks",
        sa.Column("cert_days_left", sa.Integer(), nullable=True, comment="HTTPS证书剩余天数"),
    )


def downgrade() -> None:
    op.drop_column("node_checks", "cert_days_left")
