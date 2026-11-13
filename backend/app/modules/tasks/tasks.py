"""通用任务执行（Celery）：任务记录更新、重试、幂等。

设计（见 plan.txt §4「库存与任务可靠性」）：

* 所有异步操作都先写 ``sync_runs`` 再经 Outbox 投递到本任务 ``execute_sync_run``，
  由它统一维护任务状态（pending → running → succeeded/failed/partial）；
* 任务体按 ``job_type`` 懒加载具体处理器（渠道 / 销售模块），不 import 到模块顶层，
  某个模块尚未就绪时只有对应任务失败，不影响 Worker 启动与路由导入；
* 手动任务执行前用 ``app.api.deps.ensure_permission`` 复核成员权限，权限撤销后立即失效；
* 暂时性错误按 Celery 重试次数上限退避重试，永久错误直接进入可处理失败状态；
* 处理器自身幂等（订单唯一键 + 库存差额），因此任务重复执行不会重复扣减。
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.db.session import center_session, tenant_session
from app.modules.tasks import instance, repository
from app.modules.tasks import service as tasks_service
from app.modules.tasks.models_tenant import SyncRun
from app.modules.tasks.retry import MAX_TASK_ATTEMPTS, is_retryable_error, retry_delay_seconds
from app.modules.tasks.tenancy import guard_manual_permission, job_tenant_session

logger = logging.getLogger("ventasync.tasks.tasks")

# job_type -> 处理器（点分路径）。处理器由各模块提供，运行时懒加载。
JOB_HANDLERS: dict[str, str] = {
    tasks_service.JOB_ORDER_SYNC: "app.modules.sales.tasks.run_order_sync",
    tasks_service.JOB_STOCK_PUSH: "app.modules.channels.tasks.run_stock_push",
    tasks_service.JOB_LISTING_SYNC: "app.modules.channels.tasks.sync_store_listings",
    tasks_service.JOB_TOKEN_REFRESH: "app.modules.channels.tasks.refresh_expiring_tokens",
}

# job_type -> 队列（在 Outbox 投递时决定，独立队列便于限流与扩容）
JOB_QUEUES: dict[str, str] = {
    tasks_service.JOB_ORDER_SYNC: "sales",
    tasks_service.JOB_STOCK_PUSH: "channel",
    tasks_service.JOB_LISTING_SYNC: "channel",
    tasks_service.JOB_TOKEN_REFRESH: "channel",
}

# job_type -> 手动执行所需权限（定时 / 回调任务使用限定能力的自动化身份）
JOB_PERMISSIONS: dict[str, str] = {
    tasks_service.JOB_ORDER_SYNC: "order.sync",
    tasks_service.JOB_STOCK_PUSH: "stock.push",
    tasks_service.JOB_LISTING_SYNC: "channel.listing.map",
    tasks_service.JOB_TOKEN_REFRESH: "store.connect",
}

# 已结束的任务状态（重复投递时直接跳过）
FINISHED_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "partial"})


def load_handler(job_type: str) -> Any:
    """按 job_type 懒加载处理器；未登记 / 未实现时抛带说明的 ``AppError``。"""
    dotted = JOB_HANDLERS.get(job_type)
    if not dotted:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"未登记的任务类型: {job_type}",
            details={"known": sorted(JOB_HANDLERS)},
        )
    module_path, _, attribute = dotted.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:  # pragma: no cover - 并行开发期
        raise AppError(
            ErrorCode.SERVICE_UNAVAILABLE,
            f"任务处理器所在模块尚未就绪: {module_path}",
        ) from exc
    handler = getattr(module, attribute, None)
    if handler is None:  # pragma: no cover - 并行开发期
        raise AppError(
            ErrorCode.SERVICE_UNAVAILABLE,
            f"任务处理器尚未实现: {dotted}",
        )
    return handler


def handler_kwargs(
    job_type: str,
    *,
    tenant_id: int,
    sync_run_id: int,
    store_id: int | None,
    requested_by: int | None,
    triggered_by: str,
) -> dict[str, Any]:
    """按处理器签名组装关键字参数（键名与各模块任务约定一致）。"""
    if job_type == tasks_service.JOB_ORDER_SYNC:
        return {
            "tenant_id": int(tenant_id),
            "sync_run_id": int(sync_run_id),
            "store_id": store_id,
            "requested_by": requested_by,
            "triggered_by": triggered_by,
        }
    if job_type == tasks_service.JOB_STOCK_PUSH:
        return {
            "tenant_id": int(tenant_id),
            "store_id": int(store_id or 0),
            "sync_run_id": int(sync_run_id),
            "requested_by": requested_by,
            "triggered_by": triggered_by,
        }
    if job_type == tasks_service.JOB_LISTING_SYNC:
        return {
            "tenant_id": int(tenant_id),
            "store_id": int(store_id or 0),
            "requested_by": requested_by,
            "sync_run_id": int(sync_run_id),
            "triggered_by": triggered_by,
        }
    return {"tenant_id": int(tenant_id), "requested_by": requested_by, "triggered_by": triggered_by}


def interpret_result(result: Any) -> tuple[str, int, str]:
    """把处理器返回值映射为 ``(任务状态, 处理数量, 错误摘要)``。"""
    if not isinstance(result, dict):
        return tasks_service.RUN_SUCCEEDED, 0, ""
    raw_status = str(result.get("status") or "").strip()
    raw_errors = result.get("errors") or 0
    errors = len(raw_errors) if isinstance(raw_errors, (list, tuple, dict)) else int(raw_errors)
    errors = max(errors, int(result.get("failed") or 0))
    processed = int(
        result.get("total_processed")
        or result.get("processed")
        or result.get("orders_created", 0) + result.get("orders_updated", 0)
        or 0
    )
    message = str(result.get("error") or result.get("error_message") or "").strip()
    if not message and errors:
        messages = result.get("messages") or []
        if not messages and isinstance(raw_errors, (list, tuple)):
            messages = raw_errors
        message = "；".join(str(item) for item in messages)[:1000]
    status = raw_status or (tasks_service.RUN_PARTIAL if errors else tasks_service.RUN_SUCCEEDED)
    if errors and status == tasks_service.RUN_SUCCEEDED:
        status = tasks_service.RUN_PARTIAL
    if status not in {tasks_service.RUN_SUCCEEDED, tasks_service.RUN_PARTIAL, tasks_service.RUN_FAILED}:
        status = tasks_service.RUN_SUCCEEDED
    return status, processed, message[:1000]


def _mark_run_rejected(tenant_id: int, sync_run_id: int, reason: str) -> None:
    """绑定校验拒绝时把任务置 failed（绕过租户上下文校验，直接按中心库路由打开租户库）。"""
    try:
        from app.modules.identity.service import load_tenant_db_route

        with center_session() as center:
            route = load_tenant_db_route(center, int(tenant_id))
        if route is None:
            return
        with tenant_session(route) as db:
            run = repository.get_sync_run(db, int(tenant_id), int(sync_run_id))
            if run is None or run.status in FINISHED_STATUSES:
                return
            tasks_service.mark_run_finished(
                db,
                run,
                status=tasks_service.RUN_FAILED,
                total_processed=int(run.total_processed or 0),
                error_message=f"任务被拒绝执行：{reason}"[:1000],
            )
            db.commit()
    except Exception:  # noqa: BLE001 - 标记失败只记录日志，不掩盖拒绝原因
        logger.warning("标记被拒绝任务为失败时出错 tenant=%s run=%s", tenant_id, sync_run_id)


def guard_task_execution(tenant_id: int, sync_run_id: int) -> dict[str, Any] | None:
    """任务执行前的实例/租户绑定校验；拒绝时返回结果并落审计与任务状态。

    * 绑定来自进程环境（``ERP_WORKER_INSTANCE_ID`` / ``ERP_WORKER_TENANT_ID``），
      **不接受任务消息里的 instance_id/tenant_id 覆盖**；
    * 租户必须 active 且实例一致、未独立部署 / 迁移中；
    * 拒绝属于永久错误：不重试，任务置 failed，写租户审计。
    """
    try:
        instance.ensure_dispatch_allowed(int(tenant_id), purpose="execute_sync_run")
        return None
    except AppError as exc:
        reason = str((exc.details or {}).get("reason") or exc.message)
        logger.warning(
            "拒绝执行任务 tenant=%s run=%s reason=%s binding=%s",
            tenant_id,
            sync_run_id,
            reason,
            instance.current_binding().describe(),
        )
        _mark_run_rejected(int(tenant_id), int(sync_run_id), reason)
        instance.audit_rejection(
            int(tenant_id),
            reason=reason,
            action="task.execution_rejected",
            summary=f"后台任务执行被拒绝：{reason}",
            extra={"sync_run_id": int(sync_run_id), **instance.current_binding().describe()},
        )
        return {"status": "rejected", "reason": reason, "tenant_id": int(tenant_id)}


def _begin_run(tenant_id: int, sync_run_id: int) -> SyncRun | None:
    """把任务标记为 running 并返回最新记录（独立短事务，避免与业务事务互相影响）。"""
    with job_tenant_session(tenant_id, automation=True) as (_context, db):
        run = repository.get_sync_run(db, tenant_id, sync_run_id)
        if run is None:
            return None
        if run.status in FINISHED_STATUSES:
            return run
        tasks_service.mark_run_started(db, run)
        db.commit()
        db.refresh(run)
        return run


def _finish_run(
    tenant_id: int,
    sync_run_id: int,
    *,
    status: str,
    total_processed: int = 0,
    error_message: str = "",
) -> None:
    with job_tenant_session(tenant_id, automation=True) as (_context, db):
        run = repository.get_sync_run(db, tenant_id, sync_run_id)
        if run is None:
            return
        tasks_service.mark_run_finished(
            db,
            run,
            status=status,
            total_processed=total_processed,
            error_message=error_message,
        )
        db.commit()


def read_celery_task_status(task_id: str) -> dict[str, Any]:
    """查询 Celery 任务状态（兼容其它模块自行生成 task_id 的 202 接口）。

    结果后端不可用 / 任务不存在时返回 ``status="unknown"``，不抛 500：
    任务状态查询是辅助信息，不应让页面整体报错。
    """
    payload: dict[str, Any] = {
        "task_id": str(task_id),
        "type": "celery",
        "status": "unknown",
        "result": None,
        "error": None,
    }
    try:
        from app.workers.celery_app import celery_app

        result = celery_app.AsyncResult(str(task_id))
        state = str(result.state)
        payload["status"] = state
        if state == "SUCCESS":
            value = result.result
            if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
                payload["result"] = value
            else:
                payload["result"] = str(value)
        elif state == "FAILURE":
            payload["error"] = str(result.result)[:1000]
    except Exception as exc:  # noqa: BLE001 - 结果后端异常不影响接口
        logger.warning("查询 Celery 任务状态失败 task_id=%s: %s", task_id, exc)
        payload["status"] = "unknown"
        payload["error"] = "任务状态暂不可查询"
    return payload


def recover_stale_runs(
    tenant_id: int, *, stale_minutes: int | None = None
) -> int:
    """回收「卡在 running」的任务（Worker 中断 / 进程被杀），标记失败可人工重试。"""
    minutes = int(stale_minutes or settings.sync_run_stale_minutes)
    recovered = 0
    with job_tenant_session(tenant_id, automation=True) as (_context, db):
        rows = repository.list_stale_running_runs(db, tenant_id, older_than_seconds=minutes * 60)
        for run in rows:
            tasks_service.mark_run_finished(
                db,
                run,
                status=tasks_service.RUN_FAILED,
                total_processed=int(run.total_processed or 0),
                error_message="任务执行中断（Worker 中断或超时），请重试",
            )
            recovered += 1
        db.commit()
    if recovered:
        logger.warning("租户 %s 回收中断任务 %s 条", tenant_id, recovered)
    return recovered


# --------------------------------------------------------------------------- Celery 任务
def _celery_task(**options: Any) -> Any:
    from app.workers.celery_app import celery_app

    return celery_app.task(**options)


@_celery_task(
    bind=True,
    name="app.modules.tasks.tasks.execute_sync_run",
    max_retries=MAX_TASK_ATTEMPTS,
    acks_late=True,
)
def execute_sync_run(
    self: Any,
    sync_run_id: int,
    tenant_id: int,
    job_type: str,
    store_id: int | None = None,
    triggered_by: str = "manual",
) -> dict[str, Any]:
    """统一任务入口：维护任务状态，调用具体处理器，按错误类型决定是否重试。"""
    # 1) 实例 / 租户绑定校验（固定绑定来自进程环境，任务消息不能覆盖）
    rejected = guard_task_execution(int(tenant_id), int(sync_run_id))
    if rejected is not None:
        return rejected
    # 2) 进入已校验的任务上下文，处理器内部可据此快速复核
    with instance.task_tenant_scope(int(tenant_id)):
        return _execute_sync_run_impl(
            self,
            sync_run_id=int(sync_run_id),
            tenant_id=int(tenant_id),
            job_type=job_type,
            store_id=store_id,
            triggered_by=triggered_by,
        )


def _execute_sync_run_impl(
    self: Any,
    *,
    sync_run_id: int,
    tenant_id: int,
    job_type: str,
    store_id: int | None = None,
    triggered_by: str = "manual",
) -> dict[str, Any]:
    """执行主体（已完成绑定校验）：读取任务事实、校验租户一致、调用处理器。"""
    run = _begin_run(int(tenant_id), int(sync_run_id))
    if run is None:
        logger.warning("任务不存在：tenant=%s run=%s", tenant_id, sync_run_id)
        return {"status": "ignored", "reason": "任务记录不存在"}
    if run.status in FINISHED_STATUSES:
        return {"status": run.status, "skipped": True}
    # 权威租户来自任务记录本身；消息参数只允许一致，不允许覆盖
    authoritative_tenant = int(run.tenant_id)
    if authoritative_tenant != int(tenant_id):
        reason = "task_tenant_mismatch"
        logger.error(
            "任务租户与消息不一致：run.tenant_id=%s message_tenant=%s run=%s",
            authoritative_tenant,
            tenant_id,
            sync_run_id,
        )
        _mark_run_rejected(authoritative_tenant, int(sync_run_id), reason)
        instance.audit_rejection(
            authoritative_tenant,
            reason=reason,
            action="task.execution_rejected",
            summary="后台任务租户与消息不一致，拒绝执行",
            extra={"sync_run_id": int(sync_run_id), "message_tenant": int(tenant_id)},
        )
        return {"status": "rejected", "reason": reason}
    actual_job_type = str(run.job_type)
    requested_by = int(run.requested_by) if run.requested_by else None
    actual_store_id = int(run.store_id) if run.store_id else None
    actual_triggered_by = str(run.triggered_by or triggered_by)

    permission = JOB_PERMISSIONS.get(actual_job_type)
    try:
        with job_tenant_session(
            authoritative_tenant, user_id=requested_by,
            automation=actual_triggered_by != "manual",
        ) as (context, db):
            tasks_service.ensure_job_enabled(db, authoritative_tenant, actual_job_type)
            if actual_triggered_by == "manual" and permission:
                guard_manual_permission(context, permission)
                from app.core.access import effective_from_context
                tasks_service.ensure_job_access(
                    effective_from_context(context), actual_job_type, actual_store_id,
                )
        handler = load_handler(actual_job_type)
        kwargs = handler_kwargs(
            actual_job_type,
            tenant_id=authoritative_tenant,
            sync_run_id=int(sync_run_id),
            store_id=actual_store_id,
            requested_by=requested_by,
            triggered_by=actual_triggered_by,
        )
        result = handler(**kwargs)
    except Exception as exc:  # noqa: BLE001 - 统一分类处理
        retryable = is_retryable_error(exc)
        retries_left = int(getattr(self.request, "retries", 0)) < int(self.max_retries or 0)
        logger.warning(
            "任务执行失败 tenant=%s run=%s job_type=%s retryable=%s retries_left=%s: %s",
            authoritative_tenant,
            sync_run_id,
            actual_job_type,
            retryable,
            retries_left,
            exc,
        )
        if retryable and retries_left:
            raise self.retry(
                exc=exc, countdown=retry_delay_seconds(int(self.request.retries) + 1)
            ) from exc
        _finish_run(
            authoritative_tenant,
            int(sync_run_id),
            status=tasks_service.RUN_FAILED,
            total_processed=0,
            error_message=f"{type(exc).__name__}: {exc}"[:1000],
        )
        raise

    status, processed, message = interpret_result(result)
    _finish_run(
        authoritative_tenant,
        int(sync_run_id),
        status=status,
        total_processed=processed,
        error_message=message,
    )
    return result if isinstance(result, dict) else {"status": status}


__all__ = [
    "FINISHED_STATUSES",
    "JOB_HANDLERS",
    "JOB_PERMISSIONS",
    "JOB_QUEUES",
    "execute_sync_run",
    "guard_task_execution",
    "handler_kwargs",
    "interpret_result",
    "load_handler",
    "recover_stale_runs",
]
