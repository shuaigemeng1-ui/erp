"""中心库迁移：平台渠道应用增加 TikTok Shop App ID。

Revision ID: center_0004
Revises: center_0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "center_0004"
down_revision = "center_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_channel_apps",
        sa.Column(
            "app_id",
            sa.String(length=64),
            nullable=True,
            comment="应用 ID（TikTok Shop Partner Center 的 App ID）",
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_channel_apps", "app_id")
