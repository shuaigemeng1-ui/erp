"""任务服务：异步任务记录、Outbox 事件、回调事件与定时配置。

跨模块固定契约（docs/architecture.md「跨模块接口」，签名不得修改）：

* ``create_sync_run``：异步操作返回 202 时先落任务记录；
* ``enqueue_outbox``：与业务数据同一租户库事务写入 ``outbox_events``。

补充约定：

* 所有 202 接口返回的 ``task_id`` 都是 ``sync_runs.id`` 的字符串形式，
  前端用 ``GET /tenants/{tenant_id}/tasks/{task_id}`` 轮询；
* 任务创建 / 重试 / 暂停调度都写审计（与业务同事务，审计模块延迟 import，
  未实现时只记录告警，不影响主流程）；
* 同一目标已有 pending 任务时复用，不重复堆积（数据库记录级去重）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.db.base import utcnow
from app.modules.tasks import repository
from app.modules.tasks.models_tenant import OutboxEvent, SyncRun, SyncSchedule, WebhookEvent

logger = logging.getLogger("ventasync.tasks.service")

# 任务类型（与渠道任务处理器一一对应，Worker 按 job_type 找执行器）
JOB_ORDER_SYNC = "order_sync"
JOB_STOCK_PUSH = "stock_push"
JOB_LISTING_SYNC = "listing_sync"
JOB_TOKEN_REFRESH = "token_refresh"
JOB_TYPES: tuple[str, ...] = (
    JOB_ORDER_SYNC,
    JOB_STOCK_PUSH,
    JOB_LISTING_SYNC,
    JOB_TOKEN_REFRESH,
)

# 触发来源（webhook_retry：回调事件补偿扫描后的重投，仍视为平台回调类自动化任务）
TRIGGERED_BY: tuple[str, ...] = (
    "manual",
    "schedule",
    "webhook",
    "webhook_retry",
    "system",
)

# 任务状态
RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_PARTIAL = "partial"
RUN_STATUSES: tuple[str, ...] = (
    RUN_PENDING,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    RUN_FAILED,
    RUN_PARTIAL,
)

# Outbox 事件类型
EVENT_SYNC_RUN_CREATED = "sync_run.created"
EVENT_ORDER_NORMALIZE = "order.normalize"
EVENT_STOCK_PUSH_REQUESTED = "stock.push.requested"

# 定时同步最短间隔（分钟），避免配置错误导致刷任务
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 24 * 60


def _validate_job_type(job_type: str) -> str:
    value = (job_type or "").strip()
    if value not in JOB_TYPES:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"未知任务类型: {job_type}",
            details={"allowed": list(JOB_TYPES)},
        )
    return value


def _validate_triggered_by(triggered_by: str) -> str:
    value = (triggered_by or "").strip()
    if value not in TRIGGERED_BY:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"未知任务触发来源: {triggered_by}",
            details={"allowed": list(TRIGGERED_BY)},
        )
    return value


# --------------------------------------------------------------------------- 固定契约
def create_sync_run(
    db: Session,
    *,
    tenant_id: int,
    job_type: str,
    store_id: int | None = None,
    triggered_by: str = "manual",
    requested_by: int | None = None,
) -> int:
    """创建任务记录（status=pending），返回任务 ID。调用方负责同事务写 Outbox。"""
    if tenant_id <= 0:
        raise AppError(ErrorCode.VALIDATION_ERROR, "tenant_id 必须为正整数")
    if store_id is not None and store_id <= 0:
        raise AppError(ErrorCode.VALIDATION_ERROR, "store_id 必须为正整数或为空")
    run = SyncRun(
        tenant_id=tenant_id,
        job_type=_validate_job_type(job_type),
        store_id=store_id,
        status=RUN_PENDING,
        triggered_by=_validate_triggered_by(triggered_by),
        requested_by=requested_by,
        total_processed=0,
        error_message="",
        cursor="",
    )
    db.add(run)
    db.flush()
    return int(run.id)


def enqueue_outbox(
    db: Session,
    *,
    tenant_id: int,
    event_type: str,
    aggregate_type: str,
    aggregate_id: int | None,
    payload: dict[str, object] | None = None,
    available_delay_seconds: int = 0,
) -> int:
    """在租户库事务内写入待投递事件，返回事件 ID。

    业务数据与事件同事务提交：事务成功则事件一定存在，事务回滚则事件不会漏出。
    ``available_delay_seconds`` 用于延迟重试（例如归一化失败后 60 秒再试）。
    """
    if tenant_id <= 0:
        raise AppError(ErrorCode.VALIDATION_ERROR, "tenant_id 必须为正整数")
    event_type_value = (event_type or "").strip()
    aggregate_type_value = (aggregate_type or "").strip()
    if not event_type_value or not aggregate_type_value:
        raise AppError(ErrorCode.VALIDATION_ERROR, "事件类型与聚合类型不能为空")
    delay = max(0, int(available_delay_seconds))
    event = OutboxEvent(
        tenant_id=tenant_id,
        event_type=event_type_value,
        aggregate_type=aggregate_type_value,
        aggregate_id=aggregate_id,
        payload=payload or {},
        status="pending",
        attempts=0,
        available_at=utcnow() + timedelta(seconds=delay),
        last_error="",
    )
    db.add(event)
    db.flush()
    return int(event.id)


# --------------------------------------------------------------------------- 任务编排
def request_sync_run(
    db: Session,
    *,
    tenant_id: int,
    job_type: str,
    store_id: int | None = None,
    triggered_by: str = "manual",
    requested_by: int | None = None,
) -> int:
    """建任务记录并投递 Outbox，返回任务 ID（202 接口直接用这个 ID）。

    同一 (任务类型, 店铺) 已有 pending 任务时直接复用，避免用户连点或回调重复
    造成同一批工作重复排队（真正防重仍由业务唯一约束保证）。
    """
    job_type_value = _validate_job_type(job_type)
    existing = repository.find_pending_sync_run(db, tenant_id, job_type_value, store_id)
    if existing is not None:
        return int(existing.id)
    run_id = create_sync_run(
        db,
        tenant_id=tenant_id,
        job_type=job_type_value,
        store_id=store_id,
        triggered_by=triggered_by,
        requested_by=requested_by,
    )
    enqueue_outbox(
        db,
        tenant_id=tenant_id,
        event_type=EVENT_SYNC_RUN_CREATED,
        aggregate_type="sync_run",
        aggregate_id=run_id,
        payload={
            "sync_run_id": run_id,
            "tenant_id": tenant_id,
            "job_type": job_type_value,
            "store_id": store_id,
            "triggered_by": triggered_by,
        },
    )
    return run_id


def retry_sync_run(
    db: Session,
    *,
    tenant_id: int,
    run_id: int,
    actor_id: int | None = None,
) -> int:
    """重试失败 / 部分成功的任务：新建一条任务记录并投递（原记录保留可追溯）。"""
    run = repository.get_sync_run(db, tenant_id, run_id)
    if run is None:
        raise AppError(ErrorCode.TASK_NOT_FOUND)
    if run.status in {RUN_PENDING, RUN_RUNNING}:
        raise AppError(ErrorCode.JOB_IN_PROGRESS)
    new_run_id = request_sync_run(
        db,
        tenant_id=tenant_id,
        job_type=run.job_type,
        store_id=run.store_id,
        triggered_by="manual",
        requested_by=actor_id,
    )
    record_audit(
        db,
        tenant_id=tenant_id,
        action="task.retry",
        resource_type="sync_run",
        resource_id=new_run_id,
        actor_id=actor_id,
        summary=f"重试任务 {run.job_type}",
        payload={"source_run_id": run_id, "job_type": run.job_type, "store_id": run.store_id},
    )
    return new_run_id


def mark_run_started(db: Session, run: SyncRun) -> None:
    run.status = RUN_RUNNING
    run.started_at = utcnow()
    run.error_message = ""
    db.flush()


def mark_run_finished(
    db: Session,
    run: SyncRun,
    *,
    status: str,
    total_processed: int = 0,
    error_message: str = "",
) -> None:
    if status not in RUN_STATUSES:
        status = RUN_FAILED
    run.status = status
    run.total_processed = int(total_processed)
    run.error_message = (error_message or "")[:1000]
    run.finished_at = utcnow()
    db.flush()


def next_run_at(now: datetime, interval_minutes: int) -> datetime:
    """下一次执行时间（纯函数，便于调度断言与测试）。"""
    minutes = min(max(int(interval_minutes), MIN_INTERVAL_MINUTES), MAX_INTERVAL_MINUTES)
    return now + timedelta(minutes=minutes)


# --------------------------------------------------------------------------- 定时配置
def ensure_sync_schedule(
    db: Session,
    *,
    tenant_id: int,
    job_type: str,
    store_id: int | None = None,
    interval_minutes: int = 30,
    enabled: bool = True,
) -> SyncSchedule:
    """确保存在一条定时配置（不存在则创建），返回现有或新建的配置。"""
    job_type_value = _validate_job_type(job_type)
    existing = repository.find_schedule(db, tenant_id, job_type_value, store_id)
    if existing is not None:
        return existing
    minutes = min(max(int(interval_minutes), MIN_INTERVAL_MINUTES), MAX_INTERVAL_MINUTES)
    schedule = SyncSchedule(
        tenant_id=tenant_id,
        job_type=job_type_value,
        store_id=store_id,
        interval_minutes=minutes,
        enabled=enabled,
        next_run_at=None,
    )
    db.add(schedule)
    db.flush()
    return schedule


def ensure_default_schedules(db: Session, *, tenant_id: int) -> list[SyncSchedule]:
    """开通 / 首次调度时补默认配置：订单同步默认 30 分钟一次。"""
    created: list[SyncSchedule] = []
    for job_type, minutes in ((JOB_ORDER_SYNC, 30),):
        created.append(
            ensure_sync_schedule(
                db,
                tenant_id=tenant_id,
                job_type=job_type,
                store_id=None,
                interval_minutes=minutes,
            )
        )
    return created


def update_sync_schedule(
    db: Session,
    *,
    tenant_id: int,
    schedule_id: int,
    enabled: bool | None = None,
    interval_minutes: int | None = None,
    actor_id: int | None = None,
) -> SyncSchedule:
    """更新定时配置（启停 / 间隔），写审计；重新启用时把 ``next_run_at`` 置空立即生效。"""
    schedule = repository.get_schedule(db, tenant_id, schedule_id)
    if schedule is None:
        raise AppError(ErrorCode.NOT_FOUND, "定时配置不存在或不可见")
    if interval_minutes is not None:
        minutes = int(interval_minutes)
        if not MIN_INTERVAL_MINUTES <= minutes <= MAX_INTERVAL_MINUTES:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"执行间隔必须在 {MIN_INTERVAL_MINUTES}~{MAX_INTERVAL_MINUTES} 分钟之间",
            )
        schedule.interval_minutes = minutes
    if enabled is not None:
        was_enabled = bool(schedule.enabled)
        schedule.enabled = bool(enabled)
        if enabled and not was_enabled:
            schedule.next_run_at = None
    db.flush()
    record_audit(
        db,
        tenant_id=tenant_id,
        action="task.schedule.update",
        resource_type="sync_schedule",
        resource_id=schedule.id,
        actor_id=actor_id,
        summary=f"更新定时配置 {schedule.job_type}",
        payload={
            "job_type": schedule.job_type,
            "store_id": schedule.store_id,
            "enabled": bool(schedule.enabled),
            "interval_minutes": schedule.interval_minutes,
        },
    )
    return schedule


# --------------------------------------------------------------------------- 回调事件
def record_webhook_event(
    db: Session,
    *,
    tenant_id: int,
    channel: str,
    store_id: int | None,
    event_type: str,
    external_event_id: str,
    signature_ok: bool,
    payload: dict[str, Any] | None = None,
) -> tuple[int, bool]:
    """记录平台回调事件并防重，返回 ``(事件ID, 是否已存在)``。

    (tenant_id, channel, external_event_id) 唯一约束是最终防重依据；并发重复回调
    用 SAVEPOINT 捕获唯一冲突后回查，不会把整个业务事务弄脏。
    """
    channel_value = (channel or "").strip()
    external_id = (external_event_id or "").strip()
    if not channel_value or not external_id:
        raise AppError(ErrorCode.VALIDATION_ERROR, "回调事件缺少渠道或事件标识")
    existing = repository.get_webhook_event_by_external(
        db, tenant_id, channel_value, external_id
    )
    if existing is not None:
        return int(existing.id), True
    event = WebhookEvent(
        tenant_id=tenant_id,
        channel=channel_value,
        store_id=store_id,
        event_type=(event_type or "")[:64],
        external_event_id=external_id[:128],
        signature_ok=bool(signature_ok),
        status="received",
        payload=payload or {},
        received_at=utcnow(),
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError:
        existing = repository.get_webhook_event_by_external(
            db, tenant_id, channel_value, external_id
        )
        if existing is None:  # pragma: no cover - 理论不可达
            raise
        return int(existing.id), True
    return int(event.id), False


def mark_webhook_event(
    db: Session,
    *,
    tenant_id: int,
    event_id: int,
    status: str = "processed",
    error: str = "",
) -> None:
    """更新回调事件处理状态（processed / ignored / failed）。"""
    event = db.get(WebhookEvent, event_id)
    if event is None or int(event.tenant_id) != int(tenant_id):
        return
    event.status = status
    event.processed_at = utcnow()
    if error:
        payload = dict(event.payload or {})
        payload["error"] = error[:500]
        event.payload = payload
    db.flush()


# --------------------------------------------------------------------------- 审计
def record_audit(
    db: Session,
    *,
    tenant_id: int,
    action: str,
    resource_type: str,
    resource_id: int | None = None,
    actor_id: int | None = None,
    summary: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    """写租户审计（延迟 import 审计契约；审计模块未实现时只告警，不影响主流程）。"""
    try:
        from app.modules.audit.service import record as record_audit_log
    except ImportError:  # pragma: no cover - 审计模块缺失时降级
        logger.warning("审计模块不可用，跳过审计: action=%s", action)
        return
    try:
        record_audit_log(
            db,
            tenant_id=tenant_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor_id=actor_id,
            summary=summary,
            payload=payload,
        )
    except NotImplementedError:  # pragma: no cover - 并行开发期审计未就绪
        logger.warning("审计服务尚未实现，跳过审计: action=%s", action)
