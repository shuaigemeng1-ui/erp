"""租户库迁移：stores 增加 ERP 店铺编码，支持先建店铺后授权。

Revision ID: tenant_0007
Revises: tenant_0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "tenant_0007"
down_revision = "tenant_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column("store_code", sa.String(length=32), nullable=True, comment="ERP 内部店铺编码"),
    )
    op.execute(
        "UPDATE stores SET store_code = CONCAT(UPPER(channel), '-', id) "
        "WHERE store_code IS NULL OR store_code = ''"
    )
    op.alter_column(
        "stores",
        "store_code",
        existing_type=sa.String(length=32),
        nullable=False,
        server_default="",
    )
    op.create_unique_constraint(
        "uq_stores_tenant_id_channel_store_code",
        "stores",
        ["tenant_id", "channel", "store_code"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_stores_tenant_id_channel_store_code", "stores", type_="unique")
    op.drop_column("stores", "store_code")
