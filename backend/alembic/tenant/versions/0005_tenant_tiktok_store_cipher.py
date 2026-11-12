"""租户库迁移：店铺凭据增加 TikTok Shop shop_cipher 密文字段。

Revision ID: tenant_0005
Revises: tenant_0004

说明：
* TikTok Shop 的 API 调用需要 shop_cipher（店铺维度，和 token 一起保存）；
* 仅新增一列，不改变已有唯一约束与外键策略；
* Shopee 店铺该字段保持空串。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "tenant_0005"
down_revision = "tenant_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "store_credentials",
        sa.Column(
            "shop_cipher_cipher",
            sa.String(length=2048),
            nullable=False,
            server_default=sa.text("''"),
            comment="店铺 cipher 密文（TikTok Shop shop_cipher；Shopee 为空）",
        ),
    )


def downgrade() -> None:
    op.drop_column("store_credentials", "shop_cipher_cipher")
