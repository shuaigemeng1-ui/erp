"""任务路由（租户库）。

* ``GET  /tenants/{tenant_id}/sync-runs``                  任务记录
* ``GET  /tenants/{tenant_id}/sync-runs/{run_id}``         任务详情
* ``POST /tenants/{tenant_id}/sync-runs/{run_id}/retry``   重试（202 + task_id）
* ``GET  /tenants/{tenant_id}/sync-schedules``             定时同步配置
* ``PUT  /tenants/{tenant_id}/sync-schedules/{id}``        启停 / 间隔
* ``GET  /tenants/{tenant_id}/tasks/{task_id}``            异步任务状态

``task_id`` 只接受已持久化的 ``sync_runs.id``，禁止查询无归属登记的 Celery UUID。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query

from app.api.deps import TenantCtx, TenantDb, require_permission
from app.core.access import effective_from_context
from app.core.errors import AppError, ErrorCode
from app.core.http import PageParams, accepted, paged, success
from app.core.permissions import Permission
from app.modules.tasks import repository
from app.modules.tasks import service as tasks_service
from app.modules.tasks.schemas import (
    SyncRunDetailOut,
    SyncRunOut,
    SyncScheduleOut,
    SyncScheduleUpdate,
    SyncSettingsUpdate,
    TaskStatusOut,
)

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["tasks"])

_TASK_READ = Depends(require_permission(Permission.TASK_READ))
_SCHEDULE_MANAGE = Depends(require_permission(Permission.TASK_SCHEDULE_MANAGE))
_SETTING_MANAGE = Depends(require_permission(Permission.TENANT_SETTING_MANAGE))


def _require_visible(context: Any, target: Any) -> None:
    effective = effective_from_context(context)
    if target is None or not tasks_service.scope_covers_target(effective, Permission.TASK_READ, target.store_id):
        raise AppError(ErrorCode.TASK_NOT_FOUND)


def _require_settings_scope(context: Any) -> None:
    if not tasks_service.scope_covers_target(
        effective_from_context(context), Permission.TENANT_SETTING_MANAGE, None,
    ):
        raise AppError(ErrorCode.PERMISSION_DENIED)


@router.get("/sync-settings", summary="租户同步总开关")
def get_sync_settings(
    tenant_ctx: TenantCtx, db: TenantDb, _permission: Any = _SETTING_MANAGE,
) -> dict[str, Any]:
    _require_settings_scope(tenant_ctx)
    return success(tasks_service.sync_settings_payload(db, tenant_ctx.tenant_id))


@router.put("/sync-settings", summary="修改租户同步总开关")
def put_sync_settings(
    tenant_ctx: TenantCtx, db: TenantDb, payload: SyncSettingsUpdate,
    _permission: Any = _SETTING_MANAGE,
) -> dict[str, Any]:
    _require_settings_scope(tenant_ctx)
    values = payload.model_dump(exclude_none=True)
    effective = effective_from_context(tenant_ctx)
    for key, job_type in (("order_sync_enabled", "order_sync"), ("stock_push_enabled", "stock_push")):
        if values.get(key):
            tasks_service.ensure_job_access(effective, job_type, None)
    result = tasks_service.update_sync_settings(
        db, tenant_ctx.tenant_id, values, actor_id=tenant_ctx.user_id,
    )
    db.commit()
    return success(result)



@router.get("/sync-runs", summary="任务记录列表")
def list_sync_runs(
    tenant_ctx: TenantCtx,
    db: TenantDb,
    pagination: PageParams = Depends(),
    job_type: str | None = Query(default=None, max_length=32, description="任务类型"),
    status: str | None = Query(default=None, max_length=32, description="任务状态"),
    store_id: int | None = Query(default=None, ge=1, description="店铺ID"),
    _permission: Any = _TASK_READ,
) -> dict[str, Any]:
    rows, total = repository.list_sync_runs(
        db,
        tenant_ctx.tenant_id,
        job_type=job_type,
        status=status,
        store_id=store_id,
        page=pagination.page,
        page_size=pagination.page_size,
        allowed_store_ids=effective_from_context(tenant_ctx).allowed_ids(Permission.TASK_READ, "stores"),
    )
    return paged(
        [SyncRunOut.model_validate(row) for row in rows],
        total,
        pagination.page,
        pagination.page_size,
    )


@router.get("/sync-runs/{run_id}", summary="任务详情")
def get_sync_run(
    tenant_ctx: TenantCtx,
    db: TenantDb,
    run_id: int = Path(ge=1, description="任务ID"),
    _permission: Any = _TASK_READ,
) -> dict[str, Any]:
    run = repository.get_sync_run(db, tenant_ctx.tenant_id, run_id)
    _require_visible(tenant_ctx, run)
    return success(SyncRunDetailOut.model_validate(run))


@router.post("/sync-runs/{run_id}/retry", status_code=202, summary="重试任务（异步）")
def retry_sync_run(
    tenant_ctx: TenantCtx,
    db: TenantDb,
    run_id: int = Path(ge=1, description="任务ID"),
    _permission: Any = _TASK_READ,
) -> Any:
    run = repository.get_sync_run(db, tenant_ctx.tenant_id, run_id)
    _require_visible(tenant_ctx, run)
    tasks_service.ensure_job_access(effective_from_context(tenant_ctx), run.job_type, run.store_id)
    new_run_id = tasks_service.retry_sync_run(
        db,
        tenant_id=tenant_ctx.tenant_id,
        run_id=run_id,
        actor_id=tenant_ctx.user_id,
    )
    db.commit()
    return accepted({"task_id": str(new_run_id)})


@router.get("/sync-schedules", summary="定时同步配置列表")
def list_sync_schedules(
    tenant_ctx: TenantCtx,
    db: TenantDb,
    job_type: str | None = Query(default=None, max_length=32),
    _permission: Any = _TASK_READ,
) -> dict[str, Any]:
    rows = repository.list_schedules(db, tenant_ctx.tenant_id, job_type=job_type,
        allowed_store_ids=effective_from_context(tenant_ctx).allowed_ids(Permission.TASK_READ, "stores"))
    return success([SyncScheduleOut.model_validate(row) for row in rows])


@router.put("/sync-schedules/{schedule_id}", summary="更新定时同步配置")
def update_sync_schedule(
    tenant_ctx: TenantCtx,
    db: TenantDb,
    payload: SyncScheduleUpdate,
    schedule_id: int = Path(ge=1, description="配置ID"),
    _permission: Any = _SCHEDULE_MANAGE,
) -> dict[str, Any]:
    current = repository.get_schedule(db, tenant_ctx.tenant_id, schedule_id)
    effective = effective_from_context(tenant_ctx)
    if current is None or not tasks_service.scope_covers_target(effective, Permission.TASK_SCHEDULE_MANAGE, current.store_id):
        raise AppError(ErrorCode.TASK_NOT_FOUND)
    tasks_service.ensure_job_access(effective, current.job_type, current.store_id)
    if payload.enabled:
        tasks_service.ensure_job_enabled(db, tenant_ctx.tenant_id, current.job_type)
    schedule = tasks_service.update_sync_schedule(
        db,
        tenant_id=tenant_ctx.tenant_id,
        schedule_id=schedule_id,
        enabled=payload.enabled,
        interval_minutes=payload.interval_minutes,
        actor_id=tenant_ctx.user_id,
    )
    db.commit()
    return success(SyncScheduleOut.model_validate(schedule))


@router.get("/tasks/{task_id}", summary="异步任务状态")
def get_task_status(
    tenant_ctx: TenantCtx,
    db: TenantDb,
    task_id: str = Path(min_length=1, max_length=64, description="任务ID"),
    _permission: Any = _TASK_READ,
) -> dict[str, Any]:
    """仅查询租户库中已登记、且位于成员可见范围内的任务。"""
    if not task_id.isdigit():
        raise AppError(ErrorCode.TASK_NOT_FOUND)
    run = repository.get_sync_run(db, tenant_ctx.tenant_id, int(task_id))
    _require_visible(tenant_ctx, run)
    result = ({"total_processed": int(run.total_processed or 0)}
              if run.status in {tasks_service.RUN_SUCCEEDED, tasks_service.RUN_PARTIAL} else None)
    return success(TaskStatusOut(
        task_id=str(run.id), type=str(run.job_type), status=run.status,
        result=result, error=str(run.error_message or "") or None,
    ))
