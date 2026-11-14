"""tenant_0017 TikTok 订单刷新队列租约令牌与失败分类。

背景（租户 Webhook 可靠性 P0）：

* 同一订单连续收到 Webhook 时只保留一行队列记录，用 ``generation`` 递增表达版本；
  Worker 领取时必须拿到 ``lease_token`` + ``generation``，确认完成前校验二者，
  旧 Worker 不得覆盖/确认新版本结果；
* ``status`` 增加 ``processing`` 时的租约过期扫描由调度进程每 15 秒负责，
  用 ``FOR UPDATE SKIP LOCKED`` 短事务领取；
* 逐单失败原因需要结构化记录（详情缺失 / 订单号不匹配 / 归一化失败等）。

本迁移只新增列与索引，不建外键；字段均带中文注释，旧行读取兼容。

Revision ID: tenant_0017
Revises: tenant_0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "tenant_0017"
down_revision = "tenant_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tiktok_order_refresh_queue",
        sa.Column(
            "lease_token",
            sa.String(length=64),
            nullable=True,
            comment="租约令牌（Worker 领取时生成；确认完成前校验，防旧 Worker 覆盖新版本）",
        ),
    )
    op.add_column(
        "tiktok_order_refresh_queue",
        sa.Column(
            "last_failure_code",
            sa.String(length=32),
            nullable=True,
            comment="最近失败分类：detail_missing/order_no_mismatch/normalize_failed/api_error/...",
        ),
    )
    op.create_index(
        "ix_tiktok_order_refresh_queue_lease",
        "tiktok_order_refresh_queue",
        ["tenant_id", "status", "lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tiktok_order_refresh_queue_lease", table_name="tiktok_order_refresh_queue"
    )
    op.drop_column("tiktok_order_refresh_queue", "last_failure_code")
    op.drop_column("tiktok_order_refresh_queue", "lease_token")
