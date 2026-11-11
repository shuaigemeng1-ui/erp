"""租户库模型：调度配置、同步任务、回调事件、事件投递（Outbox）。

实现负责人：backend-sales（tasks 模块）。表与字段必须带中文注释，禁止外键。

设计要点（见 plan.txt §4「库存与任务可靠性」）：

* ``outbox_events`` 与业务数据同一租户库事务写入，独立 Worker 轮询后投递到 Celery，
  避免「事务已提交但消息丢失」；投递失败记录 ``attempts`` / ``last_error`` 并退避重试。
* ``sync_runs`` 是异步任务的数据库事实：所有 202 接口返回的 ``task_id`` 就是本表主键，
  状态由 Worker 更新，不依赖 Celery result backend 才能查询。
* ``sync_schedules`` 由独立调度进程扫描，按 ``next_run_at`` 生成 ``sync_runs``。
* ``webhook_events`` 以 (tenant_id, channel, external_event_id) 唯一约束做回调防重。
* 队列消息只带 tenant_id 等标识，不携带明文凭据；Worker 重新建立 TenantContext。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    IdMixin,
    TenantBase,
    TenantIdMixin,
    TimestampMixin,
    table_options,
    utcnow,
)


class SyncSchedule(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """定时同步配置表：每个 (租户, 任务类型, 店铺) 只有一条配置。"""

    __tablename__ = "sync_schedules"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "job_type",
            "store_id",
            name="uq_sync_schedules_tenant_job_store",
        ),
        Index("ix_sync_schedules_tenant_next", "tenant_id", "next_run_at"),
        Index("ix_sync_schedules_enabled_next", "enabled", "next_run_at"),
        table_options("定时同步配置表"),
    )

    job_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="任务类型（order_sync/stock_push/listing_sync/token_refresh）",
    )
    store_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="店铺ID（为空表示租户内全部店铺）"
    )
    interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, comment="执行间隔（分钟）"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
        comment="是否启用",
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="上次生成任务时间（UTC）"
    )
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="下次应执行时间（UTC）"
    )


class SyncRun(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """同步 / 异步任务记录表：同时作为 202 接口返回的 task_id 事实来源。"""

    __tablename__ = "sync_runs"
    __table_args__ = (
        Index(
            "ix_sync_runs_tenant_job_created",
            "tenant_id",
            "job_type",
            "created_at",
        ),
        Index("ix_sync_runs_tenant_status", "tenant_id", "status"),
        Index("ix_sync_runs_tenant_store", "tenant_id", "store_id"),
        table_options("同步任务记录表"),
    )

    job_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="任务类型（order_sync/stock_push/listing_sync/token_refresh）",
    )
    store_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="店铺ID（为空表示租户内全部店铺）"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        comment="任务状态（pending/running/succeeded/failed/partial）",
    )
    triggered_by: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="manual",
        comment="触发来源（manual/schedule/webhook/system）",
    )
    requested_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="发起人用户ID（自动化任务为空）"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="开始执行时间（UTC）"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="结束时间（UTC）"
    )
    total_processed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已处理对象数量"
    )
    error_message: Mapped[str] = mapped_column(
        String(1024), nullable=False, default="", comment="失败原因（成功时为空）"
    )
    cursor: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", comment="分页游标（断点续传用）"
    )


class WebhookEvent(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """平台回调事件表：验签后先落库防重，再交给业务处理。"""

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel",
            "external_event_id",
            name="uq_webhook_events_tenant_channel_event",
        ),
        Index("ix_webhook_events_tenant_status", "tenant_id", "status"),
        table_options("平台回调事件表"),
    )

    channel: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="渠道编码（shopee 等）"
    )
    store_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="店铺ID（无法确定时为空）"
    )
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="事件类型（平台推送类型）"
    )
    external_event_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="平台事件唯一标识（防重键）"
    )
    signature_ok: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
        comment="签名校验是否通过",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="received",
        comment="处理状态（received/processed/ignored/failed）",
    )
    payload: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="回调原始数据（JSON，已脱敏）"
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="接收时间（UTC）"
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="处理完成时间（UTC）"
    )


class OutboxEvent(IdMixin, TimestampMixin, TenantIdMixin, TenantBase):
    """待投递事件表（Outbox）：与业务同事务写入，独立 Worker 投递到 Celery。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_events_status_available", "status", "available_at"),
        Index("ix_outbox_events_tenant_status", "tenant_id", "status"),
        table_options("待投递事件表"),
    )

    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="事件类型（派发路由键）"
    )
    aggregate_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="聚合类型（sync_run/store/order 等）"
    )
    aggregate_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="聚合ID"
    )
    payload: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="事件负载（不含明文凭据）"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        comment="投递状态（pending/dispatched/failed）",
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已尝试投递次数"
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        comment="可投递时间（UTC，失败后按退避推迟）",
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="投递成功时间（UTC）"
    )
    last_error: Mapped[str] = mapped_column(
        String(1024), nullable=False, default="", comment="最近一次投递失败原因"
    )
