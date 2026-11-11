"""任务模块仓储层：只做 SQLAlchemy 查询，不写业务规则、不开事务。

规则（见 docs/architecture.md「后端目录与分层」）：

* 所有租户库查询都带 ``tenant_id`` 条件；
* 不在这里 commit / rollback，事务由 Service 管理；
* 列表与总数使用同一过滤条件（数据范围在 Service 层确定）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.modules.tasks.models_tenant import (
    OutboxEvent,
    SyncRun,
    SyncSchedule,
    WebhookEvent,
)


def get_sync_run(db: Session, tenant_id: int, run_id: int) -> SyncRun | None:
    return db.execute(
        select(SyncRun).where(SyncRun.tenant_id == tenant_id, SyncRun.id == run_id)
    ).scalar_one_or_none()


def _apply_run_filters(
    stmt: Select[Any],
    tenant_id: int,
    *,
    job_type: str | None,
    status: str | None,
    store_id: int | None,
) -> Select[Any]:
    stmt = stmt.where(SyncRun.tenant_id == tenant_id)
    if job_type:
        stmt = stmt.where(SyncRun.job_type == job_type)
    if status:
        stmt = stmt.where(SyncRun.status == status)
    if store_id is not None:
        stmt = stmt.where(SyncRun.store_id == store_id)
    return stmt


def list_sync_runs(
    db: Session,
    tenant_id: int,
    *,
    job_type: str | None = None,
    status: str | None = None,
    store_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[SyncRun], int]:
    base = _apply_run_filters(
        select(SyncRun), tenant_id, job_type=job_type, status=status, store_id=store_id
    )
    total = int(
        db.execute(
            select(func.count()).select_from(base.subquery())
        ).scalar_one()
        or 0
    )
    rows = (
        db.execute(
            base.order_by(SyncRun.id.desc())
            .offset(max(0, (page - 1) * page_size))
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return list(rows), total


def find_pending_sync_run(
    db: Session, tenant_id: int, job_type: str, store_id: int | None
) -> SyncRun | None:
    """同一目标已有待执行任务时复用，避免重复堆积（数据库记录级去重）。"""
    stmt = select(SyncRun).where(
        SyncRun.tenant_id == tenant_id,
        SyncRun.job_type == job_type,
        SyncRun.status == "pending",
    )
    stmt = stmt.where(SyncRun.store_id.is_(None)) if store_id is None else stmt.where(
        SyncRun.store_id == store_id
    )
    return db.execute(stmt.order_by(SyncRun.id.asc()).limit(1)).scalar_one_or_none()


def get_schedule(db: Session, tenant_id: int, schedule_id: int) -> SyncSchedule | None:
    return db.execute(
        select(SyncSchedule).where(
            SyncSchedule.tenant_id == tenant_id, SyncSchedule.id == schedule_id
        )
    ).scalar_one_or_none()


def list_schedules(
    db: Session, tenant_id: int, *, job_type: str | None = None
) -> list[SyncSchedule]:
    stmt = select(SyncSchedule).where(SyncSchedule.tenant_id == tenant_id)
    if job_type:
        stmt = stmt.where(SyncSchedule.job_type == job_type)
    return list(db.execute(stmt.order_by(SyncSchedule.id.asc())).scalars().all())


def due_schedules(
    db: Session, tenant_id: int, now: datetime, limit: int = 100
) -> list[SyncSchedule]:
    """到点且启用的调度配置（``next_run_at`` 为空视为立即执行）。"""
    stmt = (
        select(SyncSchedule)
        .where(
            SyncSchedule.tenant_id == tenant_id,
            SyncSchedule.enabled.is_(True),
        )
        .where(
            (SyncSchedule.next_run_at.is_(None)) | (SyncSchedule.next_run_at <= now)
        )
        .order_by(SyncSchedule.next_run_at.asc(), SyncSchedule.id.asc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def find_schedule(
    db: Session, tenant_id: int, job_type: str, store_id: int | None
) -> SyncSchedule | None:
    stmt = select(SyncSchedule).where(
        SyncSchedule.tenant_id == tenant_id, SyncSchedule.job_type == job_type
    )
    stmt = stmt.where(SyncSchedule.store_id.is_(None)) if store_id is None else stmt.where(
        SyncSchedule.store_id == store_id
    )
    return db.execute(stmt.order_by(SyncSchedule.id.asc()).limit(1)).scalar_one_or_none()


def pending_outbox_events(
    db: Session, *, now: datetime, limit: int, tenant_id: int | None = None
) -> list[OutboxEvent]:
    """待投递事件：``FOR UPDATE SKIP LOCKED`` 支持多 Worker 并发领取互不阻塞。"""
    stmt = (
        select(OutboxEvent)
        .where(
            OutboxEvent.status == "pending",
            OutboxEvent.available_at <= now,
        )
        .order_by(OutboxEvent.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if tenant_id is not None:
        stmt = stmt.where(OutboxEvent.tenant_id == tenant_id)
    return list(db.execute(stmt).scalars().all())


def find_pending_outbox(    db: Session,
    tenant_id: int,
    event_type: str,
    aggregate_type: str,
    aggregate_id: int,
) -> OutboxEvent | None:
    """查找同一聚合上尚未投递的事件，用于合并（例如同一店铺的库存推送）。"""
    return db.execute(
        select(OutboxEvent)
        .where(
            OutboxEvent.tenant_id == tenant_id,
            OutboxEvent.event_type == event_type,
            OutboxEvent.aggregate_type == aggregate_type,
            OutboxEvent.aggregate_id == aggregate_id,
            OutboxEvent.status == "pending",
        )
        .order_by(OutboxEvent.id.asc())
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()


def get_webhook_event_by_external(
    db: Session, tenant_id: int, channel: str, external_event_id: str
) -> WebhookEvent | None:
    return db.execute(
        select(WebhookEvent).where(
            WebhookEvent.tenant_id == tenant_id,
            WebhookEvent.channel == channel,
            WebhookEvent.external_event_id == external_event_id,
        )
    ).scalar_one_or_none()


def list_stale_running_runs(
    db: Session, tenant_id: int, *, older_than_seconds: int
) -> list[SyncRun]:
    """执行中但长时间没有进展的任务（Worker 中断恢复用）。"""
    threshold = utcnow() - timedelta(seconds=max(60, int(older_than_seconds)))
    stmt = (
        select(SyncRun)
        .where(
            SyncRun.tenant_id == tenant_id,
            SyncRun.status == "running",
            SyncRun.started_at.is_not(None),
            SyncRun.started_at <= threshold,
        )
        .order_by(SyncRun.id.asc())
    )
    return list(db.execute(stmt).scalars().all())
