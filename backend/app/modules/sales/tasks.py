"""销售 Worker 任务（Celery）：拉单、归一化、库存影响、库存推送入队。

任务清单：

* ``run_order_sync``：拉单主流程（任务类型 ``order_sync``）
  1. 重建租户上下文；手动任务先复核 ``order.sync`` 权限与店铺数据范围；
  2. 通过 ``channels.service.fetch_orders`` 读取平台订单（函数内延迟 import）；
  3. 调 ``sales.service.ingest_platform_orders`` 落原始数据并归一化，
     订单、库存影响、库存推送 Outbox 在同一事务提交；
  4. 逐页推进游标并把进度写回 ``sync_runs``，失败保留已处理数量，重试从上次游标继续；
* ``normalize_order``：单笔订单归一化重试（原始数据已在 ``platform_order_raw``）；
* ``retry_stock_effects``：未完成库存影响的重试（Worker 中断恢复）。

外部平台请求一律在数据库事务提交后执行；任务消息只带 ID，不带明文凭据。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.core.access import effective_from_context
from app.core.context import require_tenant_context
from app.core.errors import AppError, ErrorCode
from app.core.redis import acquire_lock, release_lock
from app.db.base import utcnow
from app.modules.sales import checkpoints
from app.modules.channels.models_tenant import Store
from app.modules.tasks import service as tasks_service
from app.db.session import tenant_session
from app.modules.sales import service as sales_service
from app.modules.tasks import repository as tasks_repository
from app.modules.tasks.tenancy import guard_manual_permission, job_tenant_session

logger = logging.getLogger("ventasync.sales.tasks")

# 单次任务最多拉取页数（防止异常游标造成无限循环）
MAX_PAGES_PER_RUN = 50
# 单页订单数量（平台一般上限 50~100）
PAGE_SIZE = 50


def _celery_task(**options: Any) -> Any:
    from app.workers.celery_app import celery_app

    return celery_app.task(**options)


def _connected_store_ids(db: Any, tenant_id: int) -> list[int]:
    """租户内已连接店铺（只读文档化字段；不依赖渠道模块内部实现）。"""
    from sqlalchemy import text

    rows = db.execute(
        text(
            """
            SELECT id FROM stores
            WHERE tenant_id = :tenant_id AND status = 'connected'
            ORDER BY id ASC
            """
        ),
        {"tenant_id": int(tenant_id)},
    ).all()
    return [int(row[0]) for row in rows]


def _target_store_ids(db: Any, tenant_id: int, store_id: int | None) -> list[int]:
    """任务目标店铺：指定店铺时只处理该店铺，否则处理全部已连接店铺。"""
    if store_id:
        return [int(store_id)]
    return _connected_store_ids(db, tenant_id)


def _allowed_store_ids(context: Any, store_ids: list[int]) -> list[int]:
    """手动任务按发起人店铺数据范围裁剪目标（权限撤销后立即缩小范围）。"""
    if getattr(context, "is_automation", False):
        return store_ids
    effective = effective_from_context(context)
    allowed = effective.allowed_ids("order.sync", "stores")
    if allowed is None:
        return store_ids
    return [item for item in store_ids if item in allowed]


def run_order_sync(
    tenant_id: int,
    sync_run_id: int | None = None,
    store_id: int | None = None,
    requested_by: int | None = None,
    triggered_by: str = "manual",
) -> dict[str, Any]:
    """订单同步主流程（由 ``tasks.tasks.execute_sync_run`` 调用）。

    返回 ``{"status","total_processed","orders_created","orders_updated","errors","messages"}``，
    由任务层据此更新 ``sync_runs``。
    """
    summary: dict[str, Any] = {
        "status": "succeeded",
        "total_processed": 0,
        "orders_created": 0,
        "orders_updated": 0,
        "stock_changed": 0,
        "errors": 0,
        "messages": [],
        "stores": 0,
    }
    automation = triggered_by != "manual"
    if not automation and not requested_by:
        raise AppError(ErrorCode.PERMISSION_DENIED, "手动同步缺少执行成员")
    with job_tenant_session(tenant_id, user_id=requested_by, automation=automation) as (
        context,
        _context_db,
    ):
        if not automation:
            guard_manual_permission(context, "order.sync")
        with tenant_session(context.db_route) as db:
            store_ids = _target_store_ids(db, tenant_id, store_id)
            store_ids = _allowed_store_ids(context, store_ids)
        if not store_ids:
            summary["messages"].append("没有可同步的已连接店铺")
            summary["status"] = "partial" if automation else "failed"
            return summary

        for target_store_id in store_ids:
            try:
                store_summary = _sync_one_store(
                    tenant_id=tenant_id,
                    store_id=target_store_id,
                    sync_run_id=sync_run_id,
                    requested_by=requested_by,
                    triggered_by=triggered_by,

                )
            except Exception as exc:  # noqa: BLE001 - 单店铺失败不影响其它店铺
                summary["errors"] += 1
                summary["messages"].append(f"店铺 {target_store_id} 同步失败: {exc}"[:300])
                logger.warning("店铺 %s 订单同步失败: %s", target_store_id, exc)
                continue
            summary["stores"] += 1
            for key in ("total_processed", "orders_created", "orders_updated", "stock_changed"):
                summary[key] += int(store_summary.get(key) or 0)
            if not store_summary.get("complete", True):
                summary["status"] = "partial"
            summary["errors"] += int(store_summary.get("errors") or 0)
            summary["messages"].extend(store_summary.get("messages") or [])
            with tenant_session(context.db_route) as progress_db:
                run = tasks_repository.get_sync_run(
                    progress_db, tenant_id, int(sync_run_id or 0)
                )
                if run is not None:
                    run.total_processed = summary["total_processed"]
                    # 真实断点保存在店铺检查点，任务级游标不混用不同店铺。
                    run.cursor = ""
                    progress_db.commit()

    if summary["errors"] and summary["total_processed"] == 0:
        summary["status"] = "failed"
    elif summary["errors"] or summary["status"] == "partial":
        summary["status"] = "partial"
    else:
        summary["status"] = "succeeded"
    if summary["messages"]:
        logger.info(
            "订单同步完成 tenant=%s run=%s 店铺=%s 处理=%s 错误=%s",
            tenant_id,
            sync_run_id,
            summary["stores"],
            summary["total_processed"],
            summary["errors"],
        )
    return summary


def _sync_one_store(
    *,
    tenant_id: int,
    store_id: int,
    sync_run_id: int | None,
    requested_by: int | None,
    triggered_by: str,
    initial_cursor: str = "",
) -> dict[str, Any]:
    """一个店铺独立锁、固定窗口和断点；仅全部页面归一化成功才推进水位。"""
    context = require_tenant_context()
    lock_name = f"orders:sync:{tenant_id}:{store_id}"
    token = acquire_lock(lock_name, ttl_seconds=900)
    if token is None:
        return {"complete": False, "errors": 0, "messages": [f"店铺 {store_id} 正在同步"]}
    try:
        return _sync_locked_store(tenant_id=tenant_id, store_id=store_id,
                                 sync_run_id=sync_run_id, requested_by=requested_by,
                                 triggered_by=triggered_by, context=context)
    finally:
        release_lock(lock_name, token)


def _sync_locked_store(*, tenant_id: int, store_id: int, sync_run_id: int | None,
                       requested_by: int | None, triggered_by: str, context: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "total_processed": 0, "orders_created": 0, "orders_updated": 0,
        "stock_changed": 0, "errors": 0, "messages": [], "cursor": "", "complete": False,
    }
    with tenant_session(context.db_route) as db:
        tasks_service.ensure_job_enabled(db, tenant_id, tasks_service.JOB_ORDER_SYNC)
        store = db.execute(select(Store).where(Store.tenant_id == tenant_id,
                                               Store.id == store_id)).scalar_one_or_none()
        if store is None or store.status != "connected":
            raise AppError(ErrorCode.STORE_NOT_CONNECTED)
        channel, environment = str(store.channel), str(store.environment)
        checkpoint = checkpoints.open_window(db, tenant_id=tenant_id, store_id=store_id,
                                              channel=channel, environment=environment, now=utcnow())
        since, until, cursor = checkpoint.window_start, checkpoint.window_end, checkpoint.cursor or None
        db.commit()
    for _page in range(MAX_PAGES_PER_RUN):
        # 每一页前重新检查功能开关和身份，停用/撤权不会继续下一次外部调用。
        try:
            with tenant_session(context.db_route) as db:
                tasks_service.ensure_job_enabled(db, tenant_id, tasks_service.JOB_ORDER_SYNC)
            if triggered_by == "manual":
                with job_tenant_session(tenant_id, user_id=requested_by, automation=False) as (fresh, _db):
                    guard_manual_permission(fresh, "order.sync")
                    if not effective_from_context(fresh).covers("order.sync", store_id=store_id):
                        raise AppError(ErrorCode.PERMISSION_DENIED, "店铺同步权限已撤销")
            page = _fetch_order_page(tenant_id=tenant_id, store_id=store_id,
                                     since=since, until=until, cursor=cursor)
        except Exception as exc:  # 已提交的前面页面不能因后续网络/权限错误丢失统计
            return _page_failure(result, cursor, exc)
        orders = list(page.get("orders") or [])
        if orders:
            try:
                with tenant_session(context.db_route) as db:
                    ingest = sales_service.ingest_platform_orders(
                        db, tenant_id=tenant_id, store_id=store_id, channel=channel,
                        payloads=orders, sync_run_id=sync_run_id, triggered_by=triggered_by,
                    )
            except Exception as exc:
                return _page_failure(result, cursor, exc)
            for target, source in (("total_processed", "total"), ("orders_created", "orders_created"),
                                   ("orders_updated", "orders_updated"), ("stock_changed", "stock_changed")):
                result[target] += int(ingest.get(source) or 0)
            result["errors"] += int(ingest.get("errors") or 0)
            result["messages"].extend(ingest.get("messages") or [])
            if ingest.get("errors"):
                # 保留当前页的旧游标，重试会安全重放本页，不把失败页跳过去。
                result["cursor"] = cursor or ""
                return result
        has_more = bool(page.get("has_more"))
        next_cursor = str(page.get("next_cursor") or "")
        if has_more and (not next_cursor or next_cursor == (cursor or "")):
            raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "平台分页未返回有效的新游标")
        complete = not has_more
        with tenant_session(context.db_route) as db:
            checkpoint = checkpoints.load_checkpoint(db, tenant_id, store_id, channel, environment)
            if checkpoint is None:
                raise AppError(ErrorCode.CONFLICT, "订单同步检查点不存在")
            checkpoints.save_page(db, checkpoint, next_cursor=next_cursor, complete=complete)
            if complete:
                sales_service.mark_store_synced(db, tenant_id=tenant_id, store_id=store_id)
            db.commit()
        result["cursor"] = next_cursor
        if complete:
            result["complete"] = True
            return result
        cursor = next_cursor
    # 本次预算用尽：保留同一窗口并投递下一批，不能当作完整成功。
    with tenant_session(context.db_route) as db:
        continuation_id = tasks_service.create_sync_run(
            db, tenant_id=tenant_id, job_type=tasks_service.JOB_ORDER_SYNC, store_id=store_id,
            triggered_by=triggered_by, requested_by=requested_by,
        )
        tasks_service.enqueue_outbox(
            db, tenant_id=tenant_id, event_type=tasks_service.EVENT_SYNC_RUN_CREATED,
            aggregate_type="sync_run", aggregate_id=continuation_id,
            payload={"tenant_id": tenant_id, "sync_run_id": continuation_id,
                     "job_type": tasks_service.JOB_ORDER_SYNC, "store_id": store_id,
                     "triggered_by": triggered_by}, available_delay_seconds=5,
        )
        db.commit()
    result["messages"].append(f"店铺 {store_id} 分页未完成，已安排续传任务 {continuation_id}")
    return result


def _page_failure(result: dict[str, Any], cursor: str | None, exc: Exception) -> dict[str, Any]:
    """保留先前已提交页面的计数和当前待处理页游标，供后续任务从断点重试。"""
    result["errors"] += 1
    result["messages"].append(f"订单分页处理失败：{exc}"[:300])
    result["cursor"] = cursor or ""
    return result


def _fetch_order_page(
    *,
    tenant_id: int,
    store_id: int,
    since: Any,
    until: Any = None,
    cursor: str | None,
) -> dict[str, Any]:
    """读取一页平台订单（延迟 import 渠道契约，未就绪时给出可重试的明确错误）。"""
    try:
        from app.modules.channels.service import fetch_orders
    except ImportError as exc:  # pragma: no cover - 并行开发期
        raise sales_service.DependencyUnavailable(
            "渠道拉单接口尚未就绪：channels.service.fetch_orders 不可导入"
        ) from exc
    context = require_tenant_context()
    with tenant_session(context.db_route) as db:
        try:
            page = fetch_orders(
                db,
                tenant_id=int(tenant_id),
                store_id=int(store_id),
                since=since,
                until=until,
                cursor=cursor,
                page_size=PAGE_SIZE,
            )
        except NotImplementedError as exc:  # pragma: no cover - 并行开发期
            raise sales_service.DependencyUnavailable(
                "渠道拉单接口尚未实现：channels.service.fetch_orders 仍为占位实现"
            ) from exc
    if not isinstance(page, dict):
        raise sales_service.DependencyUnavailable("渠道拉单接口返回格式不合法")
    page.setdefault("orders", [])
    page.setdefault("next_cursor", None)
    page.setdefault("has_more", False)
    return page


def normalize_order(
    tenant_id: int,
    raw_id: int,
    sync_run_id: int | None = None,
    triggered_by: str = "system",
) -> dict[str, Any]:
    """单笔订单归一化重试（Outbox 事件 ``order.normalize`` 触发，幂等）。"""
    automation = triggered_by != "manual"
    with job_tenant_session(tenant_id, automation=automation) as (context, _db):
        with tenant_session(context.db_route) as db:
            result = sales_service.normalize_order_from_raw(
                db,
                tenant_id=tenant_id,
                raw_id=int(raw_id),
                sync_run_id=sync_run_id,
                source="sync",
            )
            db.commit()
    return result


def retry_stock_effects(tenant_id: int, limit: int = 100) -> dict[str, Any]:
    """重试未完成 / 失败的库存影响（幂等：按目标与已应用数量差额执行）。"""
    with job_tenant_session(tenant_id, automation=True) as (context, _db):
        with tenant_session(context.db_route) as db:
            result = sales_service.retry_pending_stock_effects(
                db, tenant_id=tenant_id, limit=limit
            )
            db.commit()
    return result


def recover_interrupted_runs(tenant_id: int) -> int:
    """回收中断任务（调度进程周期调用）。"""
    from app.modules.tasks.tasks import recover_stale_runs

    return recover_stale_runs(tenant_id)


@_celery_task(name="app.modules.sales.tasks.sync_orders", bind=True, max_retries=3)
def sync_orders(
    self: Any,
    tenant_id: int,
    sync_run_id: int,
    store_id: int | None = None,
    requested_by: int | None = None,
    triggered_by: str = "manual",
) -> dict[str, Any]:
    """Celery 包装：供调试或其它模块按任务名直接投递（正常路径走 execute_sync_run）。"""
    try:
        return run_order_sync(
            tenant_id=tenant_id,
            sync_run_id=sync_run_id,
            store_id=store_id,
            requested_by=requested_by,
            triggered_by=triggered_by,
        )
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("订单同步任务失败 tenant=%s run=%s: %s", tenant_id, sync_run_id, exc)
        raise


@_celery_task(name="app.modules.sales.tasks.normalize_order", max_retries=5)
def normalize_order_task(
    tenant_id: int,
    raw_id: int,
    sync_run_id: int | None = None,
) -> dict[str, Any]:
    return normalize_order(tenant_id, raw_id, sync_run_id=sync_run_id)


@_celery_task(name="app.modules.sales.tasks.retry_stock_effects", max_retries=3)
def retry_stock_effects_task(tenant_id: int, limit: int = 100) -> dict[str, Any]:
    return retry_stock_effects(tenant_id, limit=limit)


__all__ = [
    "normalize_order",
    "normalize_order_task",
    "recover_interrupted_runs",
    "retry_stock_effects",
    "retry_stock_effects_task",
    "run_order_sync",
    "sync_orders",
]
