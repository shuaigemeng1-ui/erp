"""租户库迁移：stores 增加 TikTok Shop 仓库ID（库存推送使用）。

Revision ID: tenant_0006
Revises: tenant_0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "tenant_0006"
down_revision = "tenant_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column(
            "warehouse_id",
            sa.String(length=64),
            nullable=True,
            comment="TikTok Shop 仓库ID（库存推送使用；Shopee 为空）",
        ),
    )


def downgrade() -> None:
    op.drop_column("stores", "warehouse_id")
