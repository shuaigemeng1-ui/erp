"""tenant_0015 店铺真实名称与平台商品主图

Revision ID: tenant_0015
Revises: tenant_0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "tenant_0015"
down_revision = "tenant_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column(
            "external_name",
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("''"),
            comment="平台店铺真实名称（只读，展示用）",
        ),
    )
    op.add_column(
        "channel_listings",
        sa.Column(
            "image_url",
            sa.String(length=1000),
            nullable=False,
            server_default=sa.text("''"),
            comment="平台商品主图 URL",
        ),
    )


def downgrade() -> None:
    op.drop_column("channel_listings", "image_url")
    op.drop_column("stores", "external_name")
