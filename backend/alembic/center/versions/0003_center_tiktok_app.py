"""中心库迁移：平台渠道应用增加 TikTok Shop 的 App Key / App Secret 字段。

Revision ID: center_0003
Revises: center_0002

说明：
* 保留 Shopee 的 partner_id / partner_key_cipher / partner_secret_cipher 字段；
* TikTok Shop 使用 app_key（明文，App Key 非敏感）与 app_secret_cipher（AES 密文）；
* 不写数据、不加外键。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "center_0003"
down_revision = "center_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_channel_apps",
        sa.Column(
            "app_key",
            sa.String(length=128),
            nullable=True,
            comment="应用 Key（TikTok Shop App Key；Shopee 用 partner_id）",
        ),
    )
    op.add_column(
        "platform_channel_apps",
        sa.Column(
            "app_secret_cipher",
            sa.String(length=1024),
            nullable=True,
            comment="应用 Secret 密文（TikTok Shop App Secret）",
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_channel_apps", "app_secret_cipher")
    op.drop_column("platform_channel_apps", "app_key")
