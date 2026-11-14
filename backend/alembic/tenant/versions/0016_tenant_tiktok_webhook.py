"""tenant_0016 TikTok Webhook 订阅与订单刷新队列。

Revision ID: tenant_0016
Revises: tenant_0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "tenant_0016"
down_revision = "tenant_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column(
            "webhook_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="是否启用平台 Webhook",
        ),
    )
    op.add_column(
        "stores",
        sa.Column(
            "webhook_last_event_at",
            sa.DateTime(),
            nullable=True,
            comment="最近一次 Webhook 事件时间（UTC）",
        ),
    )
    op.create_table(
        "tiktok_webhook_subscriptions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("store_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("callback_url", sa.String(length=500), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'enabled'")),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tiktok_webhook_subscriptions"),
        sa.UniqueConstraint(
            "tenant_id", "store_id", "event_type",
            name="uq_tiktok_webhook_subscriptions_scope",
        ),
        comment="TikTok Webhook 订阅表",
    )
    op.create_index(
        "ix_tiktok_webhook_subscriptions_tenant_store",
        "tiktok_webhook_subscriptions",
        ["tenant_id", "store_id"],
        unique=False,
    )
    op.create_table(
        "tiktok_order_refresh_queue",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("store_id", sa.BigInteger(), nullable=False),
        sa.Column("external_order_no", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("processed_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tiktok_order_refresh_queue"),
        sa.UniqueConstraint(
            "tenant_id", "store_id", "external_order_no",
            name="uq_tiktok_order_refresh_queue_scope",
        ),
        comment="TikTok 订单刷新队列表",
    )
    op.create_index(
        "ix_tiktok_order_refresh_queue_due",
        "tiktok_order_refresh_queue",
        ["tenant_id", "status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tiktok_order_refresh_queue_due", table_name="tiktok_order_refresh_queue")
    op.drop_table("tiktok_order_refresh_queue")
    op.drop_index(
        "ix_tiktok_webhook_subscriptions_tenant_store",
        table_name="tiktok_webhook_subscriptions",
    )
    op.drop_table("tiktok_webhook_subscriptions")
    op.drop_column("stores", "webhook_last_event_at")
    op.drop_column("stores", "webhook_enabled")
