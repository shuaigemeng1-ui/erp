"""任务路由（租户库）。

* ``GET  /tenants/{tenant_id}/sync-runs``                  任务记录
* ``GET  /tenants/{tenant_id}/sync-runs/{run_id}``         任务详情
* ``POST /tenants/{tenant_id}/sync-runs/{run_id}/retry``   重试（202 + task_id）
* ``GET  /tenants/{tenant_id}/sync-schedules``             定时同步配置
* ``PUT  /tenants/{tenant_id}/sync-schedules/{id}``        启停 / 间隔
* ``GET  /tenants/{tenant_id}/tasks/{task_id}``            异步任务状态

``task_id`` 优先按 ``sync_runs`` 解析（本系统 202 接口返回值）；纯数字之外的 ID
按 Celery 任务解析，兼容其它模块自行生成 uuid 的 202 接口。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query

from app.api.deps import TenantCtx, TenantDb, require_permission
from app.core.errors import AppError, ErrorCode
from app.core.http import PageParams, accepted, paged, success
from app.core.permissions import Permission
from app.modules.tasks import repository
from app.modules.tasks import service as tasks_service
from app.modules.tasks import tasks as tasks_runner
from app.modules.tasks.schemas import (
    SyncRunDetailOut,
    SyncRunOut,
    SyncScheduleOut,
    SyncScheduleUpdate,
    TaskStatusOut,
)

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["tasks"])

_TASK_READ = Depends(require_permission(Permission.TASK_READ))


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
    if run is None:
        raise AppError(ErrorCode.TASK_NOT_FOUND)
    return success(SyncRunDetailOut.model_validate(run))


@router.post("/sync-runs/{run_id}/retry", status_code=202, summary="重试任务（异步）")
def retry_sync_run(
    tenant_ctx: TenantCtx,
    db: TenantDb,
    run_id: int = Path(ge=1, description="任务ID"),
    _permission: Any = _TASK_READ,
) -> Any:
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
    rows = repository.list_schedules(db, tenant_ctx.tenant_id, job_type=job_type)
    return success([SyncScheduleOut.model_validate(row) for row in rows])


@router.put("/sync-schedules/{schedule_id}", summary="更新定时同步配置")
def update_sync_schedule(
    tenant_ctx: TenantCtx,
    db: TenantDb,
    payload: SyncScheduleUpdate,
    schedule_id: int = Path(ge=1, description="配置ID"),
    _permission: Any = _TASK_READ,
) -> dict[str, Any]:
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
) -> dict[str, Any]:
    """成员可查询的任务状态：同步任务读数据库，Celery 任务读结果后端。"""
    tenant_id = tenant_ctx.tenant_id
    if task_id.isdigit():
        run = repository.get_sync_run(db, tenant_id, int(task_id))
        if run is None:
            raise AppError(ErrorCode.TASK_NOT_FOUND)
        status = run.status
        result: Any | None = None
        if run.status == tasks_service.RUN_SUCCEEDED or run.status == tasks_service.RUN_PARTIAL:
            result = {"total_processed": int(run.total_processed or 0)}
        return success(
            TaskStatusOut(
                task_id=str(run.id),
                type=str(run.job_type),
                status=status,
                result=result,
                error=str(run.error_message or "") or None,
            )
        )

    payload = tasks_runner.read_celery_task_status(task_id)
    # 跨租户防护：Celery 结果若带 tenant_id，必须与路径租户一致，否则按不可见处理
    result_value = payload.get("result")
    if isinstance(result_value, dict) and result_value.get("tenant_id") is not None:
        try:
            result_tenant = int(result_value["tenant_id"])
        except (TypeError, ValueError):
            result_tenant = -1
        if result_tenant != tenant_id:
            raise AppError(ErrorCode.TASK_NOT_FOUND)
    return success(TaskStatusOut(**payload))
