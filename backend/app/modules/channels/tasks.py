"""渠道 Worker 任务（Celery）：商品同步、订单拉取、令牌刷新、库存推送。

可靠性约定（plan.txt §4「库存与任务可靠性」）：

* Worker 内必须重建 ``TenantContext`` 才能访问租户库；任务消息只带 tenant / store /
  sku / 操作者等业务键，不携带明文凭据。
* 手动任务执行前复核成员权限（``required_permission``）；定时任务使用自动化身份
  （``is_automation=True``）。
* 只对暂时性错误做有限重试；凭据失效直接进入可处理失败状态。
* 库存推送按 store+sku 串行、执行时读取最新库存（由 Service 内的 Redis 锁保证）。
* Outbox 由 tasks 模块统一派发，本模块不额外注册路由。

任务名是跨模块契约（sales / tasks 通过 send_task 按名字投递），勿随意改名：

* ``app.modules.channels.tasks.sync_store_listings``
* ``app.modules.channels.tasks.run_order_sync``
* ``app.modules.channels.tasks.refresh_store_token``
* ``app.modules.channels.tasks.refresh_expiring_tokens``
* ``app.modules.channels.tasks.run_stock_push``
* ``app.modules.channels.tasks.push_stock``
* ``app.modules.channels.tasks.push_stock_for_sku``
* ``app.modules.channels.tasks.compensate_webhook_events``

回调补偿接入点：``app/modules/tasks/scheduler.py`` 调度进程在每个租户提交事务后同步调用
``compensate_tenant_webhook_events(tenant_id)``；补偿任务也可手动 / 通过 Celery 触发。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from celery import Task
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.context import (
    TenantContext,
    reset_tenant_context,
    set_tenant_context,
)
from app.core.errors import AppError, ErrorCode
from app.core.permissions import Permission
from app.core.security import utc_now
from app.db.session import center_session, tenant_session
from app.modules.channels import repository as repo
from app.modules.channels import service
from app.modules.channels.constants import CHANNEL_SHOPEE, STORE_STATUS_CONNECTED
from app.modules.channels.shopee.client import ShopeeApiError
from app.workers.celery_app import celery_app

logger = logging.getLogger("ventasync.channels.tasks")

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 30


# --------------------------------------------------------------------------- 任务上下文
def tenant_owner_user_id(center: Session, tenant_id: int) -> int | None:
    """读取租户负责人用户ID（自动化任务以负责人身份运行并标记 ``is_automation``）。"""
    try:
        row = center.execute(
            text("SELECT owner_user_id FROM tenants WHERE id = :tenant_id"),
            {"tenant_id": tenant_id},
        ).first()
    except Exception:  # noqa: BLE001 - 中心库未就绪 / 字段缺失时返回 None
        logger.warning("读取租户负责人失败：tenant=%s", tenant_id)
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


@contextmanager
def job_tenant_context(
    tenant_id: int,
    *,
    actor_user_id: int | None = None,
    required_permission: str | None = None,
) -> Iterator[tuple[TenantContext, Session]]:
    """在 Worker 内重建不可变 ``TenantContext`` 并打开租户库 Session。

    * ``actor_user_id`` 为空表示自动化任务（使用租户负责人身份，标记 ``is_automation``）；
    * 手动任务（有操作者）执行前复核成员与功能权限，防止停用 / 撤权后继续越权。
    """
    from app.modules.access.service import load_access_snapshot
    from app.modules.identity.service import load_tenant_access, load_tenant_db_route

    with center_session() as center:
        effective_user_id = actor_user_id or tenant_owner_user_id(center, tenant_id)
        if not effective_user_id:
            raise AppError(ErrorCode.TENANT_NOT_FOUND, "租户不存在或缺少负责人")
        try:
            access = load_tenant_access(center, tenant_id, effective_user_id)
            route = load_tenant_db_route(center, tenant_id)
        except NotImplementedError as exc:
            raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "身份服务尚未就绪") from exc
        if access is None:
            raise AppError(ErrorCode.TENANT_NOT_FOUND)
        if access.tenant_status == "suspended":
            raise AppError(ErrorCode.TENANT_SUSPENDED)
        if access.tenant_status != "active":
            raise AppError(ErrorCode.TENANT_NOT_ACTIVE)
        if access.member_status != "active":
            raise AppError(ErrorCode.PERMISSION_DENIED, "成员已停用")
        if route is None:
            raise AppError(ErrorCode.TENANT_DATABASE_UNAVAILABLE)
    with tenant_session(route) as db:
        try:
            snapshot = load_access_snapshot(db, tenant_id, access.member_id)
        except NotImplementedError as exc:
            raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "权限服务尚未就绪") from exc
        if (
            required_permission
            and actor_user_id is not None
            and not snapshot.effective.has(required_permission)
        ):
            raise AppError(ErrorCode.PERMISSION_DENIED)
        context = TenantContext(
            tenant_id=tenant_id,
            tenant_code=access.tenant_code,
            tenant_name=access.tenant_name,
            tenant_status=access.tenant_status,
            timezone=access.timezone,
            member_id=access.member_id,
            member_status=access.member_status,
            user_id=effective_user_id,
            db_route=route,
            permission_version=snapshot.permission_version,
            permissions=snapshot.effective.permissions,
            scope_grants=snapshot.effective.grants,
            is_automation=actor_user_id is None,
            actor_id=actor_user_id or effective_user_id,
            feature_flags=snapshot.feature_flags,
        )
        token = set_tenant_context(context)
        try:
            yield context, db
        finally:
            reset_tenant_context(token)


# --------------------------------------------------------------------------- 重试策略
def _should_retry(exc: Exception) -> bool:
    """仅暂时性错误（平台 5xx / 超时 / 限流 / 服务暂不可用）允许重试。"""
    if isinstance(exc, ShopeeApiError):
        return bool(exc.transient)
    if isinstance(exc, AppError):
        return exc.code == ErrorCode.SERVICE_UNAVAILABLE
    return False


def _retry(task: Task, exc: Exception) -> None:
    retries = int(getattr(task.request, "retries", 0) or 0)
    if _should_retry(exc) and retries < MAX_RETRIES:
        raise task.retry(exc=exc, countdown=RETRY_BACKOFF_SECONDS * (retries + 1))
    raise exc


# --------------------------------------------------------------------------- 商品同步
@celery_app.task(bind=True, name="app.modules.channels.tasks.sync_store_listings", max_retries=MAX_RETRIES)
def sync_store_listings(
    self: Task,
    tenant_id: int,
    store_id: int,
    requested_by: int | None = None,
    sync_run_id: int | None = None,
    triggered_by: str = "manual",
) -> dict[str, Any]:
    """同步店铺平台商品 / 规格并自动匹配本地 SKU。"""
    if triggered_by == "manual" and requested_by is None:
        raise AppError(ErrorCode.PERMISSION_DENIED, "手动任务缺少操作者")
    try:
        with job_tenant_context(
            tenant_id,
            actor_user_id=requested_by,
            required_permission=(
                Permission.CHANNEL_LISTING_MAP.value if requested_by else None
            ),
        ) as (context, db):
            from app.modules.tasks.service import ensure_job_access
            from app.core.access import effective_from_context
            if requested_by:
                ensure_job_access(effective_from_context(context), "listing_sync", store_id)
            with center_session() as center:
                result = service.sync_store_listings(
                    center, db, context.tenant_id, store_id, actor_id=requested_by
                )
            logger.info(
                "平台商品同步完成：tenant=%s store=%s result=%s sync_run=%s",
                tenant_id,
                store_id,
                result,
                sync_run_id,
            )
            return {"tenant_id": tenant_id, "store_id": store_id, **result}
    except Exception as exc:  # noqa: BLE001 - 统一按暂时性错误决定是否重试
        _retry(self, exc)
        raise


# --------------------------------------------------------------------------- 订单拉取
def _enqueue_sales_ingest(
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    payloads: list[dict[str, Any]],
    sync_run_id: int | None,
) -> dict[str, Any] | None:
    """把拉取的原始订单交给 sales 归一化（未实现时记录日志，不崩溃）。"""
    if not payloads:
        return {"ingested": 0}
    try:
        from app.modules.sales.service import ingest_platform_orders
    except ImportError:
        logger.warning("sales 模块未就绪，跳过订单归一化：tenant=%s store=%s", tenant_id, store_id)
        return None
    try:
        return dict(
            ingest_platform_orders(
                db,
                tenant_id=tenant_id,
                store_id=store_id,
                channel=CHANNEL_SHOPEE,
                payloads=payloads,
                sync_run_id=sync_run_id,
            )
            or {}
        )
    except NotImplementedError:
        logger.warning("sales.service.ingest_platform_orders 尚未实现，跳过订单归一化")
        return None


@celery_app.task(bind=True, name="app.modules.channels.tasks.run_order_sync", max_retries=MAX_RETRIES)
def run_order_sync(self: Task, tenant_id: int, store_id: int | None = None,
                   sync_run_id: int | None = None, requested_by: int | None = None,
                   triggered_by: str = "webhook") -> dict[str, Any]:
    """兼容旧任务名，统一进入带断点和权限门禁的销售同步器。"""
    from app.modules.sales.tasks import run_order_sync as synchronize
    return synchronize(tenant_id=tenant_id, store_id=store_id, sync_run_id=sync_run_id,
                       requested_by=requested_by, triggered_by=triggered_by)


# --------------------------------------------------------------------------- 令牌刷新
def _refresh_store_token_impl(tenant_id: int, store_id: int) -> dict[str, Any]:
    with job_tenant_context(tenant_id) as (context, db):
        with center_session() as center:
            service.refresh_store_token(center, db, context.tenant_id, store_id)
    return {"tenant_id": tenant_id, "store_id": store_id, "refreshed": True}


@celery_app.task(bind=True, name="app.modules.channels.tasks.refresh_store_token", max_retries=MAX_RETRIES)
def refresh_store_token(self: Task, tenant_id: int, store_id: int) -> dict[str, Any]:
    """按店铺串行刷新 Shopee 令牌（凭据失效标记重新授权）。"""
    try:
        return _refresh_store_token_impl(tenant_id, store_id)
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc)
        raise


@celery_app.task(bind=True, name="app.modules.channels.tasks.refresh_expiring_tokens")
def refresh_expiring_tokens(self: Task, tenant_id: int, requested_by: int | None = None, triggered_by: str = "schedule") -> dict[str, Any]:
    """扫描租户内即将过期的店铺凭据并逐个刷新。"""
    if triggered_by == "manual" and requested_by is None:
        raise AppError(ErrorCode.PERMISSION_DENIED)
    result: dict[str, Any] = {"tenant_id": tenant_id, "refreshed": 0, "failed": 0, "errors": []}
    with job_tenant_context(tenant_id, actor_user_id=requested_by, required_permission="store.connect") as (context, db):
        targets = service.list_refresh_targets(db, context.tenant_id)
        if requested_by:
            from app.core.access import effective_from_context
            allowed = effective_from_context(context).allowed_ids("store.connect", "stores")
            if allowed is not None:
                targets = [item for item in targets if item in allowed]
    for store_id in targets:
        try:
            _refresh_store_token_impl(tenant_id, store_id)
            result["refreshed"] += 1
        except AppError as exc:
            result["failed"] += 1
            result["errors"].append({"store_id": store_id, "code": exc.code.value})
        except Exception as exc:  # noqa: BLE001 - 单店铺失败不阻断其他店铺
            logger.exception("刷新店铺令牌失败：tenant=%s store=%s", tenant_id, store_id)
            result["failed"] += 1
            result["errors"].append({"store_id": store_id, "code": "INTERNAL_ERROR", "message": str(exc)})
    return result


# --------------------------------------------------------------------------- 库存推送
def _run_stock_push_impl(
    tenant_id: int,
    store_id: int,
    *,
    sync_run_id: int | None = None,
    sku_ids: list[int] | None = None,
    requested_by: int | None = None,
    triggered_by: str = "schedule",
) -> dict[str, Any]:
    if triggered_by == "manual" and requested_by is None:
        raise AppError(ErrorCode.PERMISSION_DENIED, "手动任务缺少操作者")
    with job_tenant_context(tenant_id, actor_user_id=requested_by if triggered_by == "manual" else None,
                            required_permission="stock.push") as (context, db):
        from app.modules.tasks.service import ensure_job_enabled, ensure_job_access
        from app.core.access import effective_from_context
        ensure_job_enabled(db, tenant_id, "stock_push")
        if triggered_by == "manual":
            ensure_job_access(effective_from_context(context), "stock_push", store_id)
        with center_session() as center:
            result = service.run_stock_push(
                center, db, context.tenant_id, store_id, sku_ids=sku_ids, actor_id=requested_by
            )
    return {
        "tenant_id": tenant_id,
        "store_id": store_id,
        "sync_run_id": sync_run_id,
        **result,
    }


@celery_app.task(bind=True, name="app.modules.channels.tasks.run_stock_push", max_retries=MAX_RETRIES)
def run_stock_push(
    self: Task,
    tenant_id: int,
    store_id: int,
    sync_run_id: int | None = None,
    sku_ids: list[int] | None = None,
    requested_by: int | None = None,
    triggered_by: str = "schedule",
) -> dict[str, Any]:
    """批量库存推送（按 store+sku 串行，执行时读取最新库存）。"""
    try:
        return _run_stock_push_impl(
            tenant_id, store_id, sync_run_id=sync_run_id, sku_ids=sku_ids,
            requested_by=requested_by, triggered_by=triggered_by
        )
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc)
        raise


@celery_app.task(bind=True, name="app.modules.channels.tasks.push_stock", max_retries=MAX_RETRIES)
def push_stock(
    self: Task, tenant_id: int, store_id: int, sku_ids: list[int] | None = None
) -> dict[str, Any]:
    """指定店铺库存推送（sku_ids 为空时推送该店铺全部已映射 SKU）。"""
    try:
        return _run_stock_push_impl(tenant_id, store_id, sku_ids=sku_ids)
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc)
        raise


@celery_app.task(bind=True, name="app.modules.channels.tasks.push_stock_for_sku", max_retries=MAX_RETRIES)
def push_stock_for_sku(self: Task, tenant_id: int, store_id: int, sku_id: int) -> dict[str, Any]:
    """单 SKU 库存推送（Outbox / sales 按需要投递）。"""
    try:
        with job_tenant_context(tenant_id) as (context, db):
            with center_session() as center:
                result = service.push_stock_for_sku(
                    center, db, context.tenant_id, store_id, sku_id
                )
            return {"tenant_id": tenant_id, "store_id": store_id, **result}
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc)
        raise


# --------------------------------------------------------------------------- Webhook 补偿
COMPENSATION_LIMIT_DEFAULT = 50
COMPENSATION_LIMIT_MAX = 200


def _compensate_webhook_event(
    event: Any, tenant_id: int, db: Session, result: dict[str, Any]
) -> None:
    """处理单个未处理事件：安全重投订单同步；失败累计 attempts，超限标记 failed。"""
    meta = service.compensation_meta(event.payload)
    try:
        attempts = int(meta.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    now = utc_now()
    error = ""
    sync_run_id: int | None = None
    if not event.store_id:
        error = "missing_store_id"
    else:
        try:
            # 只创建同步任务（不直接归一化订单）：sales 侧按店铺复用 pending 任务，
            # 订单 / 库存归一化本身按 (tenant, store, external_order_no) 幂等，
            # 因此重复补偿不会重复扣减库存。
            sync_run_id = service.enqueue_order_sync(
                db, tenant_id, int(event.store_id), triggered_by="webhook_retry"
            )
            if sync_run_id is None:
                error = "sales_order_sync_unavailable"
        except AppError as exc:
            error = f"{exc.code.value}: {exc.message}"
        except Exception as exc:  # noqa: BLE001 - 单事件异常不影响同批其他事件
            logger.exception(
                "推送补偿单事件失败：tenant=%s event=%s", tenant_id, getattr(event, "id", None)
            )
            error = f"{exc.__class__.__name__}: {exc}"[:500]
    if sync_run_id is not None:
        event.status = "processed"
        event.processed_at = now
        event.updated_at = now
        event.payload = service.merge_compensation_meta(
            event.payload,
            attempts=attempts,
            last_error="",
            attempted_at=now,
            last_result="processed",
        )
        db.commit()
        result["processed"] += 1
        return
    plan = service.plan_compensation_attempt(attempts, error)
    # enqueue 内部可能抛数据库异常导致会话不可用：先回滚并重新加载事件行，
    # 再写补偿元数据（只写 status / processed_at / payload，不改表结构）。
    try:
        db.rollback()
        refreshed = db.get(type(event), int(event.id)) if getattr(event, "id", None) else None
        if refreshed is not None:
            event = refreshed
    except Exception:  # noqa: BLE001 - 重新加载失败时仍尝试写回原对象
        logger.exception(
            "推送补偿回滚/重载事件失败：tenant=%s event=%s",
            tenant_id,
            getattr(event, "id", None),
        )
    event.status = plan["status"]
    event.updated_at = now
    if plan["failed"]:
        event.processed_at = now
    event.payload = service.merge_compensation_meta(
        event.payload,
        attempts=plan["attempts"],
        last_error=error,
        attempted_at=now,
        last_result=plan["status"],
    )
    db.commit()
    if plan["failed"]:
        result["failed"] += 1
        logger.warning(
            "推送补偿达到上限，标记 failed：tenant=%s event=%s attempts=%s error=%s",
            tenant_id,
            getattr(event, "id", None),
            plan["attempts"],
            error,
        )
    else:
        result["retried"] += 1


def compensate_tenant_webhook_events(
    tenant_id: int, limit: int = COMPENSATION_LIMIT_DEFAULT
) -> dict[str, Any]:
    """补偿单个租户的未处理推送事件（同步函数，调度进程 / Celery 任务共用）。

    扫描 ``webhook_events`` 中 ``status='received'`` 且超过 2 分钟的订单类事件，
    用 ``FOR UPDATE SKIP LOCKED`` 降低并发领取冲突；只更新 status/processed_at/payload
    三列（表归 tasks/sales 模块，不新增列，attempts 记录在 payload 元数据中）。
    单租户异常自吞并记录日志，返回 dict 供调度进程汇总。
    """
    result: dict[str, Any] = {
        "tenant_id": int(tenant_id),
        "scanned": 0,
        "processed": 0,
        "retried": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
    }
    try:
        from app.modules.tasks.models_tenant import WebhookEvent
    except (ImportError, AttributeError):
        logger.warning("webhook_events 模型不可用，跳过推送补偿：tenant=%s", tenant_id)
        result["skipped"] = 1
        return result
    cutoff = utc_now() - timedelta(seconds=service.WEBHOOK_COMPENSATION_MIN_AGE_SECONDS)
    batch = max(1, min(int(limit), COMPENSATION_LIMIT_MAX))
    try:
        with job_tenant_context(tenant_id) as (context, db):
            statement = (
                select(WebhookEvent)
                .where(
                    WebhookEvent.tenant_id == context.tenant_id,
                    WebhookEvent.channel == CHANNEL_SHOPEE,
                    WebhookEvent.status == "received",
                    WebhookEvent.event_type.in_(
                        tuple(service.COMPENSATION_ORDER_EVENT_TYPES)
                    ),
                    WebhookEvent.received_at.is_not(None),
                    WebhookEvent.received_at <= cutoff,
                )
                .order_by(WebhookEvent.received_at.asc())
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
            events = list(db.execute(statement).scalars().all())
            result["scanned"] = len(events)
            for event in events:
                _compensate_webhook_event(event, context.tenant_id, db, result)
    except AppError as exc:
        logger.warning(
            "推送补偿跳过：tenant=%s code=%s message=%s",
            tenant_id,
            exc.code.value,
            exc.message,
        )
        result["errors"].append({"code": exc.code.value, "message": exc.message})
    except Exception:  # noqa: BLE001 - 单租户失败不影响调度进程
        logger.exception("推送补偿失败：tenant=%s", tenant_id)
        result["errors"].append({"code": "INTERNAL_ERROR"})
    return result


@celery_app.task(
    name="app.modules.channels.tasks.compensate_webhook_events",
    max_retries=0,
)
def compensate_webhook_events(
    tenant_id: int | None = None, limit: int = COMPENSATION_LIMIT_DEFAULT
) -> dict[str, Any]:
    """补偿入口：指定租户或遍历全部 active 租户（手动 / 运维重投用）。"""
    if tenant_id is not None:
        return {"tenants": 1, **compensate_tenant_webhook_events(int(tenant_id), limit)}
    try:
        from app.modules.tasks.tenancy import active_tenant_ids

        tenant_ids = list(active_tenant_ids())
    except (ImportError, NotImplementedError):
        with center_session() as center:
            rows = center.execute(
                text("SELECT id FROM tenants WHERE status = 'active' ORDER BY id")
            ).all()
        tenant_ids = [int(row[0]) for row in rows]
    summary: dict[str, Any] = {
        "tenants": 0,
        "scanned": 0,
        "processed": 0,
        "retried": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
    }
    for current_tenant_id in tenant_ids:
        one = compensate_tenant_webhook_events(int(current_tenant_id), limit)
        summary["tenants"] += 1
        for key in ("scanned", "processed", "retried", "failed", "skipped"):
            summary[key] += int(one.get(key) or 0)
        summary["errors"].extend(one.get("errors") or [])
    return summary


# --------------------------------------------------------------------------- Outbox 注册
def register_outbox_routes(register) -> None:
    """Outbox 事件路由注册钩子。

    ``sync_run.created`` 由 tasks 模块统一派发到 ``execute_sync_run``，渠道模块不需要
    额外事件路由；保留该函数供 ``app/modules/tasks/outbox.py`` 启动时调用。
    """
    logger.debug("channels.tasks.register_outbox_routes: 无需额外注册 Outbox 路由")


# --------------------------------------------------------------------------- 投递工具
def dispatch(task: Task, **kwargs: Any) -> str:
    """按任务对象投递 Celery 任务，返回 task_id（参数全部使用关键字，避免顺序错位）。"""
    task_id = uuid.uuid4().hex
    result = task.apply_async(kwargs=kwargs, task_id=task_id)
    return str(getattr(result, "id", "") or task_id)


__all__ = [
    "compensate_tenant_webhook_events",
    "compensate_webhook_events",
    "dispatch",
    "job_tenant_context",
    "push_stock",
    "push_stock_for_sku",
    "refresh_expiring_tokens",
    "refresh_store_token",
    "register_outbox_routes",
    "run_order_sync",
    "run_stock_push",
    "sync_store_listings",
    "tenant_owner_user_id",
]
