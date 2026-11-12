"""中心库迁移：TikTok Shop 应用增加授权市场 region（ROW/US）。

Revision ID: center_0005
Revises: center_0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "center_0005"
down_revision = "center_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_channel_apps",
        sa.Column(
            "region",
            sa.String(length=16),
            nullable=True,
            server_default="ROW",
            comment="TikTok Shop 授权市场：ROW/US",
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_channel_apps", "region")
