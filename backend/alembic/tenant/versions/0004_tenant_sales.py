"""租户库迁移：订单、订单明细、状态历史、平台原始数据、订单库存影响、任务与 Outbox。

Revision ID: tenant_0004
Revises: tenant_0003

约定（plan.txt §2「无外键规范」）：

* 只建 ``orders``、``order_items``、``order_status_history``、``platform_order_raw``、
  ``order_stock_effects``、``sync_schedules``、``sync_runs``、``webhook_events``、
  ``outbox_events`` 九张表，禁止外键、禁止数据库级联删除；
* 订单以 ``(tenant_id, store_id, external_order_no)`` 唯一约束防重；平台原始数据同键防重，
  只保留最新一份，便于归一化失败后重试；
* 订单明细以 ``(tenant_id, order_id, external_item_id, external_sku_id)`` 唯一；
  库存影响以 ``(tenant_id, order_item_id, stock_place_id)`` 唯一，保证差额幂等；
* 回调事件以 ``(tenant_id, channel, external_event_id)`` 唯一防重；
* 所有表 / 字段带中文 COMMENT，UTF-8 编码。

实现负责人：backend-sales。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Alembic 版本标识（Lead 固定，勿改）
revision = "tenant_0004"
down_revision = "tenant_0003"
branch_labels = None
depends_on = None

_TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def _base_columns() -> list[sa.Column]:
    """每张表统一的审计列：主键 + 创建/更新时间（UTC）+ 租户ID。"""
    return [
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
    ]


def upgrade() -> None:
    # ------------------------------------------------------------------ 平台原始订单
    op.create_table(
        "platform_order_raw",
        *_base_columns(),
        sa.Column("channel", sa.String(length=32), nullable=False, comment="渠道编码（shopee 等）"),
        sa.Column("store_id", sa.BigInteger(), nullable=False, comment="店铺ID"),
        sa.Column("external_order_no", sa.String(length=64), nullable=False, comment="平台订单号"),
        sa.Column("payload", sa.JSON(), nullable=False, comment="平台返回的原始订单数据（JSON）"),
        sa.Column("fetched_at", sa.DateTime(), nullable=False, comment="最近一次抓取时间（UTC）"),
        sa.PrimaryKeyConstraint("id", name="pk_platform_order_raw"),
        sa.UniqueConstraint(
            "tenant_id",
            "store_id",
            "external_order_no",
            name="uq_platform_order_raw_tenant_store_order",
        ),
        comment="平台订单原始数据表",
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_platform_order_raw_tenant_id", "platform_order_raw", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_platform_order_raw_tenant_store_order",
        "platform_order_raw",
        ["tenant_id", "store_id", "external_order_no"],
        unique=False,
    )

    # ------------------------------------------------------------------ 统一订单
    op.create_table(
        "orders",
        *_base_columns(),
        sa.Column("store_id", sa.BigInteger(), nullable=False, comment="店铺ID"),
        sa.Column("channel", sa.String(length=32), nullable=False, comment="渠道编码（shopee 等）"),
        sa.Column("external_order_no", sa.String(length=64), nullable=False, comment="平台订单号"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="归一化订单状态（pending/paid/shipped/completed/cancelled/unknown）",
        ),
        sa.Column(
            "platform_status", sa.String(length=64), nullable=False, comment="平台原始订单状态"
        ),
        sa.Column("currency", sa.String(length=8), nullable=False, comment="币种（ISO 4217）"),
        sa.Column(
            "total_amount",
            sa.Numeric(18, 4),
            nullable=False,
            comment="订单总额（平台币种，DECIMAL）",
        ),
        sa.Column("buyer_name", sa.String(length=128), nullable=False, comment="买家名称"),
        sa.Column("placed_at", sa.DateTime(), nullable=True, comment="平台下单时间（UTC）"),
        sa.Column("raw_id", sa.BigInteger(), nullable=True, comment="对应平台原始数据ID"),
        sa.Column(
            "abnormal_reason",
            sa.String(length=512),
            nullable=False,
            comment="异常原因（未映射SKU/未知状态/负库存等，非空表示需人工处理）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.UniqueConstraint(
            "tenant_id",
            "store_id",
            "external_order_no",
            name="uq_orders_tenant_store_external_no",
        ),
        comment="统一订单表",
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_orders_tenant_id", "orders", ["tenant_id"], unique=False)
    op.create_index(
        "ix_orders_tenant_status_placed",
        "orders",
        ["tenant_id", "status", "placed_at"],
        unique=False,
    )
    op.create_index(
        "ix_orders_tenant_store_placed",
        "orders",
        ["tenant_id", "store_id", "placed_at"],
        unique=False,
    )

    # ------------------------------------------------------------------ 订单明细
    op.create_table(
        "order_items",
        *_base_columns(),
        sa.Column("order_id", sa.BigInteger(), nullable=False, comment="订单ID"),
        sa.Column(
            "external_item_id",
            sa.String(length=64),
            nullable=False,
            comment="平台商品ID",
        ),
        sa.Column(
            "external_sku_id",
            sa.String(length=128),
            nullable=False,
            comment="平台规格/卖家SKU编码",
        ),
        sa.Column("sku_id", sa.BigInteger(), nullable=True, comment="本地SKU ID（未映射时为空）"),
        sa.Column("item_name", sa.String(length=255), nullable=False, comment="商品名称快照"),
        sa.Column("quantity", sa.Integer(), nullable=False, comment="购买数量"),
        sa.Column("unit_price", sa.Numeric(18, 4), nullable=False, comment="成交单价（DECIMAL）"),
        sa.Column(
            "discount_amount", sa.Numeric(18, 4), nullable=False, comment="明细优惠金额（DECIMAL）"
        ),
        sa.Column("item_status", sa.String(length=32), nullable=False, comment="平台明细状态"),
        sa.Column(
            "mapping_status",
            sa.String(length=32),
            nullable=False,
            comment="SKU 映射状态（mapped/missing/ambiguous）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_items"),
        sa.UniqueConstraint(
            "tenant_id",
            "order_id",
            "external_item_id",
            "external_sku_id",
            name="uq_order_items_tenant_order_external",
        ),
        comment="订单明细表",
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_order_items_tenant_id", "order_items", ["tenant_id"], unique=False)
    op.create_index(
        "ix_order_items_tenant_order", "order_items", ["tenant_id", "order_id"], unique=False
    )

    # ------------------------------------------------------------------ 订单状态历史
    op.create_table(
        "order_status_history",
        *_base_columns(),
        sa.Column("order_id", sa.BigInteger(), nullable=False, comment="订单ID"),
        sa.Column(
            "from_status",
            sa.String(length=32),
            nullable=False,
            comment="变更前归一化状态（新建订单为空）",
        ),
        sa.Column(
            "to_status", sa.String(length=32), nullable=False, comment="变更后归一化状态"
        ),
        sa.Column(
            "platform_status",
            sa.String(length=64),
            nullable=False,
            comment="变更时平台原始状态",
        ),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            comment="变更来源（sync/webhook/manual/system）",
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False, comment="变更时间（UTC）"),
        sa.PrimaryKeyConstraint("id", name="pk_order_status_history"),
        comment="订单状态历史表",
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_order_status_history_tenant_id", "order_status_history", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_order_status_history_tenant_order",
        "order_status_history",
        ["tenant_id", "order_id", "changed_at"],
        unique=False,
    )

    # ------------------------------------------------------------------ 订单库存影响
    op.create_table(
        "order_stock_effects",
        *_base_columns(),
        sa.Column("order_id", sa.BigInteger(), nullable=False, comment="订单ID"),
        sa.Column("order_item_id", sa.BigInteger(), nullable=False, comment="订单明细ID"),
        sa.Column("sku_id", sa.BigInteger(), nullable=False, comment="本地SKU ID"),
        sa.Column("stock_place_id", sa.BigInteger(), nullable=False, comment="库存地点ID"),
        sa.Column(
            "applied_quantity",
            sa.Integer(),
            nullable=False,
            comment="已应用的库存影响数量（正数=已扣减）",
        ),
        sa.Column(
            "target_quantity",
            sa.Integer(),
            nullable=False,
            comment="目标库存影响数量（正数=应扣减）",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="影响状态（pending/applied/skipped/failed）",
        ),
        sa.Column(
            "last_task_id",
            sa.String(length=64),
            nullable=False,
            comment="最近一次处理的任务标识",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_stock_effects"),
        sa.UniqueConstraint(
            "tenant_id",
            "order_item_id",
            "stock_place_id",
            name="uq_order_stock_effects_tenant_item_place",
        ),
        comment="订单库存影响表",
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_order_stock_effects_tenant_id", "order_stock_effects", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_order_stock_effects_tenant_order",
        "order_stock_effects",
        ["tenant_id", "order_id"],
        unique=False,
    )
    op.create_index(
        "ix_order_stock_effects_tenant_status",
        "order_stock_effects",
        ["tenant_id", "status"],
        unique=False,
    )

    # ------------------------------------------------------------------ 定时同步配置
    op.create_table(
        "sync_schedules",
        *_base_columns(),
        sa.Column(
            "job_type",
            sa.String(length=32),
            nullable=False,
            comment="任务类型（order_sync/stock_push/listing_sync/token_refresh）",
        ),
        sa.Column(
            "store_id", sa.BigInteger(), nullable=True, comment="店铺ID（为空表示租户内全部店铺）"
        ),
        sa.Column("interval_minutes", sa.Integer(), nullable=False, comment="执行间隔（分钟）"),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
            comment="是否启用",
        ),
        sa.Column("last_run_at", sa.DateTime(), nullable=True, comment="上次生成任务时间（UTC）"),
        sa.Column("next_run_at", sa.DateTime(), nullable=True, comment="下次应执行时间（UTC）"),
        sa.PrimaryKeyConstraint("id", name="pk_sync_schedules"),
        sa.UniqueConstraint(
            "tenant_id", "job_type", "store_id", name="uq_sync_schedules_tenant_job_store"
        ),
        comment="定时同步配置表",
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_sync_schedules_tenant_id", "sync_schedules", ["tenant_id"], unique=False)
    op.create_index(
        "ix_sync_schedules_tenant_next",
        "sync_schedules",
        ["tenant_id", "next_run_at"],
        unique=False,
    )
    op.create_index(
        "ix_sync_schedules_enabled_next",
        "sync_schedules",
        ["enabled", "next_run_at"],
        unique=False,
    )

    # ------------------------------------------------------------------ 任务记录
    op.create_table(
        "sync_runs",
        *_base_columns(),
        sa.Column(
            "job_type",
            sa.String(length=32),
            nullable=False,
            comment="任务类型（order_sync/stock_push/listing_sync/token_refresh）",
        ),
        sa.Column(
            "store_id", sa.BigInteger(), nullable=True, comment="店铺ID（为空表示租户内全部店铺）"
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="任务状态（pending/running/succeeded/failed/partial）",
        ),
        sa.Column(
            "triggered_by",
            sa.String(length=32),
            nullable=False,
            comment="触发来源（manual/schedule/webhook/system）",
        ),
        sa.Column(
            "requested_by", sa.BigInteger(), nullable=True, comment="发起人用户ID（自动化任务为空）"
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True, comment="开始执行时间（UTC）"),
        sa.Column("finished_at", sa.DateTime(), nullable=True, comment="结束时间（UTC）"),
        sa.Column("total_processed", sa.Integer(), nullable=False, comment="已处理对象数量"),
        sa.Column(
            "error_message", sa.String(length=1024), nullable=False, comment="失败原因（成功时为空）"
        ),
        sa.Column(
            "cursor", sa.String(length=255), nullable=False, comment="分页游标（断点续传用）"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sync_runs"),
        comment="同步任务记录表",
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_sync_runs_tenant_id", "sync_runs", ["tenant_id"], unique=False)
    op.create_index(
        "ix_sync_runs_tenant_job_created",
        "sync_runs",
        ["tenant_id", "job_type", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_sync_runs_tenant_status", "sync_runs", ["tenant_id", "status"], unique=False
    )
    op.create_index("ix_sync_runs_tenant_store", "sync_runs", ["tenant_id", "store_id"], unique=False)

    # ------------------------------------------------------------------ 回调事件
    op.create_table(
        "webhook_events",
        *_base_columns(),
        sa.Column("channel", sa.String(length=32), nullable=False, comment="渠道编码（shopee 等）"),
        sa.Column("store_id", sa.BigInteger(), nullable=True, comment="店铺ID（无法确定时为空）"),
        sa.Column(
            "event_type",
            sa.String(length=64),
            nullable=False,
            comment="事件类型（平台推送类型）",
        ),
        sa.Column(
            "external_event_id",
            sa.String(length=128),
            nullable=False,
            comment="平台事件唯一标识（防重键）",
        ),
        sa.Column(
            "signature_ok",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
            comment="签名校验是否通过",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="处理状态（received/processed/ignored/failed）",
        ),
        sa.Column("payload", sa.JSON(), nullable=True, comment="回调原始数据（JSON，已脱敏）"),
        sa.Column("received_at", sa.DateTime(), nullable=False, comment="接收时间（UTC）"),
        sa.Column("processed_at", sa.DateTime(), nullable=True, comment="处理完成时间（UTC）"),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_events"),
        sa.UniqueConstraint(
            "tenant_id",
            "channel",
            "external_event_id",
            name="uq_webhook_events_tenant_channel_event",
        ),
        comment="平台回调事件表",
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_webhook_events_tenant_id", "webhook_events", ["tenant_id"], unique=False)
    op.create_index(
        "ix_webhook_events_tenant_status", "webhook_events", ["tenant_id", "status"], unique=False
    )

    # ------------------------------------------------------------------ Outbox 事件
    op.create_table(
        "outbox_events",
        *_base_columns(),
        sa.Column("event_type", sa.String(length=64), nullable=False, comment="事件类型（派发路由键）"),
        sa.Column(
            "aggregate_type",
            sa.String(length=64),
            nullable=False,
            comment="聚合类型（sync_run/store/order 等）",
        ),
        sa.Column("aggregate_id", sa.BigInteger(), nullable=True, comment="聚合ID"),
        sa.Column("payload", sa.JSON(), nullable=True, comment="事件负载（不含明文凭据）"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="投递状态（pending/dispatched/failed）",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, comment="已尝试投递次数"),
        sa.Column(
            "available_at",
            sa.DateTime(),
            nullable=False,
            comment="可投递时间（UTC，失败后按退避推迟）",
        ),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True, comment="投递成功时间（UTC）"),
        sa.Column(
            "last_error", sa.String(length=1024), nullable=False, comment="最近一次投递失败原因"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        comment="待投递事件表",
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_outbox_events_tenant_id", "outbox_events", ["tenant_id"], unique=False)
    op.create_index(
        "ix_outbox_events_status_available",
        "outbox_events",
        ["status", "available_at"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_events_tenant_status", "outbox_events", ["tenant_id", "status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_tenant_status", table_name="outbox_events")
    op.drop_index("ix_outbox_events_status_available", table_name="outbox_events")
    op.drop_index("ix_outbox_events_tenant_id", table_name="outbox_events")
    op.drop_table("outbox_events")

    op.drop_index("ix_webhook_events_tenant_status", table_name="webhook_events")
    op.drop_index("ix_webhook_events_tenant_id", table_name="webhook_events")
    op.drop_table("webhook_events")

    op.drop_index("ix_sync_runs_tenant_store", table_name="sync_runs")
    op.drop_index("ix_sync_runs_tenant_status", table_name="sync_runs")
    op.drop_index("ix_sync_runs_tenant_job_created", table_name="sync_runs")
    op.drop_index("ix_sync_runs_tenant_id", table_name="sync_runs")
    op.drop_table("sync_runs")

    op.drop_index("ix_sync_schedules_enabled_next", table_name="sync_schedules")
    op.drop_index("ix_sync_schedules_tenant_next", table_name="sync_schedules")
    op.drop_index("ix_sync_schedules_tenant_id", table_name="sync_schedules")
    op.drop_table("sync_schedules")

    op.drop_index("ix_order_stock_effects_tenant_status", table_name="order_stock_effects")
    op.drop_index("ix_order_stock_effects_tenant_order", table_name="order_stock_effects")
    op.drop_index("ix_order_stock_effects_tenant_id", table_name="order_stock_effects")
    op.drop_table("order_stock_effects")

    op.drop_index("ix_order_status_history_tenant_order", table_name="order_status_history")
    op.drop_index("ix_order_status_history_tenant_id", table_name="order_status_history")
    op.drop_table("order_status_history")

    op.drop_index("ix_order_items_tenant_order", table_name="order_items")
    op.drop_index("ix_order_items_tenant_id", table_name="order_items")
    op.drop_table("order_items")

    op.drop_index("ix_orders_tenant_store_placed", table_name="orders")
    op.drop_index("ix_orders_tenant_status_placed", table_name="orders")
    op.drop_index("ix_orders_tenant_id", table_name="orders")
    op.drop_table("orders")

    op.drop_index("ix_platform_order_raw_tenant_store_order", table_name="platform_order_raw")
    op.drop_index("ix_platform_order_raw_tenant_id", table_name="platform_order_raw")
    op.drop_table("platform_order_raw")
