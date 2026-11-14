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
from sqlalchemy import and_, or_, select, text
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
from app.modules.channels import service
from app.modules.channels.constants import CHANNEL_SHOPEE, CHANNEL_TIKTOK
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





# --------------------------------------------------------------------------- TikTok Webhook order refresh
TIKTOK_REFRESH_LEASE_SECONDS = 120
TIKTOK_REFRESH_MAX_ATTEMPTS = 8
TIKTOK_REFRESH_WAKE_LIMIT = 50


class TiktokRefreshError(Exception):
    """订单刷新失败（逐单记录分类：detail_missing/order_no_mismatch/normalize_failed）。"""

    def __init__(self, failure_code: str, message: str = "") -> None:
        self.failure_code = str(failure_code)[:32]
        self.message = str(message or failure_code)[:500]
        super().__init__(self.message)


def _refresh_due_condition(now: Any) -> Any:
    """待领取条件：pending/retry_wait 到期，或 processing 但租约已过期。"""
    from app.modules.channels.models_tenant import TiktokOrderRefreshQueue

    return or_(
        and_(
            TiktokOrderRefreshQueue.status.in_(("pending", "retry_wait")),
            or_(
                TiktokOrderRefreshQueue.next_attempt_at.is_(None),
                TiktokOrderRefreshQueue.next_attempt_at <= now,
            ),
        ),
        and_(
            TiktokOrderRefreshQueue.status == "processing",
            TiktokOrderRefreshQueue.lease_expires_at.is_not(None),
            TiktokOrderRefreshQueue.lease_expires_at <= now,
        ),
    )


def claim_tiktok_refresh_batch(
    db: Session,
    tenant_id: int,
    *,
    now: Any | None = None,
    limit: int = 20,
    lease_owner: str = "tiktok-webhook",
    lease_seconds: int = TIKTOK_REFRESH_LEASE_SECONDS,
) -> list[dict[str, Any]]:
    """短事务领取：SELECT ... FOR UPDATE SKIP LOCKED + 写入 lease_token/generation。

    返回 claim 列表；调用方提交事务后即可在外呼前释放行锁。lease_token 与 generation
    由 Worker 保存，确认/失败写回前必须同时匹配。
    """
    from datetime import datetime, timezone

    from app.modules.channels.models_tenant import TiktokOrderRefreshQueue

    timestamp = now or datetime.now(timezone.utc).replace(tzinfo=None)
    batch = max(1, min(int(limit), 50))
    rows = list(
        db.execute(
            select(TiktokOrderRefreshQueue)
            .where(
                TiktokOrderRefreshQueue.tenant_id == int(tenant_id),
                _refresh_due_condition(timestamp),
            )
            .order_by(TiktokOrderRefreshQueue.id.asc())
            .limit(batch)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    claims: list[dict[str, Any]] = []
    for row in rows:
        token = uuid.uuid4().hex
        row.status = "processing"
        row.lease_owner = str(lease_owner)[:64]
        row.lease_token = token
        row.lease_expires_at = timestamp + timedelta(seconds=max(10, int(lease_seconds)))
        row.attempts = int(row.attempts or 0) + 1
        claims.append(
            {
                "queue_id": int(row.id),
                "tenant_id": int(row.tenant_id),
                "store_id": int(row.store_id),
                "external_order_no": str(row.external_order_no),
                "generation": int(row.generation or 0),
                "lease_token": token,
                "attempts": int(row.attempts or 0),
            }
        )
    db.flush()
    return claims


def _locked_refresh_row(db: Session, claim: dict[str, Any]) -> Any | None:
    from app.modules.channels.models_tenant import TiktokOrderRefreshQueue

    return db.execute(
        select(TiktokOrderRefreshQueue)
        .where(
            TiktokOrderRefreshQueue.id == int(claim["queue_id"]),
            TiktokOrderRefreshQueue.tenant_id == int(claim["tenant_id"]),
        )
        .with_for_update()
    ).scalar_one_or_none()


def refresh_claim_is_current(db: Session, claim: dict[str, Any]) -> bool:
    """校验租约与 generation 是否仍属于本次领取（外呼返回后、写订单前调用）。"""
    row = _locked_refresh_row(db, claim)
    if row is None:
        return False
    return (
        str(row.status or "") == "processing"
        and str(row.lease_token or "") == str(claim["lease_token"])
        and int(row.generation or 0) == int(claim["generation"])
    )


def confirm_tiktok_refresh_success(
    db: Session,
    claim: dict[str, Any],
    *,
    processed_order_ids: list[str] | None = None,
) -> bool:
    """只确认已处理版本：token/generation/status 全部匹配才写 idle 并标记事件 processed。"""
    row = _locked_refresh_row(db, claim)
    if row is None:
        return False
    if (
        str(row.status or "") != "processing"
        or str(row.lease_token or "") != str(claim["lease_token"])
        or int(row.generation or 0) != int(claim["generation"])
    ):
        logger.info(
            "TikTok refresh confirm skipped (stale version): queue=%s gen=%s/%s",
            claim.get("queue_id"),
            claim.get("generation"),
            getattr(row, "generation", None),
        )
        return False
    row.status = "idle"
    row.processed_generation = int(claim["generation"])
    row.attempts = 0
    row.next_attempt_at = None
    row.lease_owner = None
    row.lease_token = None
    row.lease_expires_at = None
    row.last_error = None
    row.last_failure_code = None
    service.mark_tiktok_webhook_events_processed(
        db,
        int(claim["tenant_id"]),
        store_id=int(claim["store_id"]),
        order_ids=list(processed_order_ids or [claim["external_order_no"]]),
    )
    db.flush()
    return True


def fail_tiktok_refresh(
    db: Session,
    claim: dict[str, Any],
    *,
    error: str,
    failure_code: str,
    now: Any | None = None,
) -> bool:
    """失败写回：仅 token/generation 匹配时生效；超限置 dead，否则退避 retry_wait。"""
    from datetime import datetime, timezone

    row = _locked_refresh_row(db, claim)
    if row is None:
        return False
    if (
        str(row.status or "") != "processing"
        or str(row.lease_token or "") != str(claim["lease_token"])
        or int(row.generation or 0) != int(claim["generation"])
    ):
        return False
    timestamp = now or datetime.now(timezone.utc).replace(tzinfo=None)
    attempts = int(row.attempts or 0)
    row.last_error = str(error or failure_code)[:1000]
    row.last_failure_code = str(failure_code or "refresh_failed")[:32]
    row.lease_owner = None
    row.lease_token = None
    row.lease_expires_at = None
    if attempts >= TIKTOK_REFRESH_MAX_ATTEMPTS:
        row.status = "dead"
        row.next_attempt_at = None
    else:
        row.status = "retry_wait"
        row.next_attempt_at = timestamp + timedelta(
            seconds=min(3600, 15 * (2 ** min(attempts, 8)))
        )
    db.flush()
    return True


def _order_identity(order: Any) -> str:
    if not isinstance(order, dict):
        return ""
    for key in ("id", "order_id", "order_sn", "main_order_id"):
        value = str(order.get(key) or "").strip()
        if value:
            return value
    return ""


def _classify_refresh_exception(exc: Exception) -> tuple[str, str]:
    """失败分类（逐单记录）：detail_missing / order_no_mismatch / normalize_failed / api_error。"""
    if isinstance(exc, TiktokRefreshError):
        return exc.failure_code, exc.message
    if isinstance(exc, AppError):
        if exc.code == ErrorCode.ORDER_NOT_FOUND:
            return "detail_missing", exc.message or "订单详情为空"
        if exc.code == ErrorCode.VALIDATION_ERROR:
            return "normalize_failed", exc.message or "订单归一化失败"
    if isinstance(exc, ShopeeApiError):  # 渠道 API 错误（TikTok client 复用同类异常）
        return "api_error", str(exc)
    return "refresh_failed", f"{type(exc).__name__}: {exc}"


@celery_app.task(
    name="app.modules.channels.tasks.process_tiktok_webhook_events",
    max_retries=0,
)
def process_tiktok_webhook_events(tenant_id: int, limit: int = 20) -> dict[str, Any]:
    """领取租户 TikTok 订单刷新队列并定向拉取订单详情（Celery 仅作为唤醒触发器）。

    * 领取：``SELECT ... FOR UPDATE SKIP LOCKED`` 短事务写 lease_token/generation；
    * 外呼：只按 external_order_no 拉取，不持锁；
    * 写订单前：校验 token/generation，旧版本直接跳过（防旧 Worker 覆盖新结果）；
    * 确认：仅匹配版本才置 idle + 标记关联 webhook 事件 processed；失败逐单记录分类。
    """
    from app.modules.sales.service import ingest_platform_orders

    result: dict[str, Any] = {
        "tenant_id": int(tenant_id),
        "scanned": 0,
        "processed": 0,
        "failed": 0,
        "stale": 0,
        "errors": [],
    }
    with job_tenant_context(int(tenant_id)) as (context, db):
        claims = claim_tiktok_refresh_batch(db, context.tenant_id, limit=limit)
        db.commit()
    result["scanned"] = len(claims)
    if not claims:
        return result

    for claim in claims:
        order_no = str(claim["external_order_no"])
        failure_code = ""
        failure_error = ""
        try:
            with job_tenant_context(int(tenant_id)) as (context, db):
                store = service.get_store_or_raise(db, context.tenant_id, int(claim["store_id"]))
                with center_session() as center:
                    client = service.build_tiktok_client(center, db, context.tenant_id, store)
                payload = client.get_order_details([order_no])
            data = payload.get("data") if isinstance(payload, dict) else None
            orders = data.get("orders") if isinstance(data, dict) else None
            if not isinstance(orders, list) or not orders:
                raise TiktokRefreshError("detail_missing", "TikTok order detail is empty")
            matched = [item for item in orders if _order_identity(item) == order_no]
            if not matched:
                raise TiktokRefreshError(
                    "order_no_mismatch",
                    f"订单号不匹配：请求 {order_no}，返回 {[_order_identity(item) for item in orders][:5]}",
                )
            with job_tenant_context(int(tenant_id)) as (context, db):
                if not refresh_claim_is_current(db, claim):
                    db.rollback()
                    result["stale"] += 1
                    continue
                summary = ingest_platform_orders(
                    db,
                    tenant_id=context.tenant_id,
                    store_id=int(claim["store_id"]),
                    channel=CHANNEL_TIKTOK,
                    payloads=matched[:1],
                    triggered_by="webhook",
                )
                if int(summary.get("errors") or 0) > 0:
                    raise TiktokRefreshError(
                        "normalize_failed",
                        "订单归一化失败：" + "；".join((summary.get("messages") or [])[:3]),
                    )
            with job_tenant_context(int(tenant_id)) as (context, db):
                confirmed = confirm_tiktok_refresh_success(
                    db, claim, processed_order_ids=[order_no]
                )
                db.commit()
            if confirmed:
                result["processed"] += 1
            else:
                result["stale"] += 1
        except Exception as exc:  # noqa: BLE001 - 单订单失败不影响其它订单
            failure_code, failure_error = _classify_refresh_exception(exc)
            logger.warning(
                "TikTok order refresh failed: tenant=%s order=%s code=%s error=%s",
                tenant_id,
                order_no,
                failure_code,
                exc,
            )
            failure_applied = False
            try:
                with job_tenant_context(int(tenant_id)) as (context, db):
                    failure_applied = fail_tiktok_refresh(
                        db,
                        claim,
                        error=failure_error,
                        failure_code=failure_code,
                    )
                    db.commit()
            except Exception:  # noqa: BLE001
                logger.exception("TikTok refresh failure state write failed")
            if failure_applied:
                result["failed"] += 1
                result["errors"].append(
                    {
                        "queue_id": claim["queue_id"],
                        "external_order_no": order_no,
                        "failure_code": failure_code,
                        "error": failure_error[:300],
                    }
                )
            else:
                # 失败写回时发现新事件已递增 generation：旧 Worker 不得覆盖新结果
                result["stale"] += 1
    return result


def scan_due_tiktok_refresh(tenant_id: int, limit: int = TIKTOK_REFRESH_WAKE_LIMIT) -> int:
    """调度进程只读扫描：用 SKIP LOCKED 数一下到期行，不领取、不写租约。"""
    from datetime import datetime, timezone

    from app.modules.channels.models_tenant import TiktokOrderRefreshQueue

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with job_tenant_context(int(tenant_id)) as (context, db):
        rows = list(
            db.execute(
                select(TiktokOrderRefreshQueue.id)
                .where(
                    TiktokOrderRefreshQueue.tenant_id == context.tenant_id,
                    _refresh_due_condition(now),
                )
                .order_by(TiktokOrderRefreshQueue.id.asc())
                .limit(max(1, min(int(limit), 200)))
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        db.rollback()
    return len(rows)


def wake_tiktok_refresh(tenant_id: int) -> dict[str, Any]:
    """调度进程唤醒：有到期行才投递 Celery 任务（Celery 只负责触发 Worker 领取）。"""
    due = scan_due_tiktok_refresh(int(tenant_id))
    woken = False
    if due > 0:
        dispatch(process_tiktok_webhook_events, tenant_id=int(tenant_id))
        woken = True
    return {"due": due, "woken": woken}


def reconcile_tiktok_webhook_events_in_db(
    db: Session, tenant_id: int, *, limit: int = 50
) -> dict[str, int]:
    """历史 received 事件按订单对账：已有业务结果补 processed，缺结果重新入队。

    只处理 ``channel=tiktok, status=received``；订单存在即认为业务结果已落库，
    缺订单时检查刷新队列：活跃中则跳过，dead/idle 或缺失则重新入队（generation 递增）。
    """
    from datetime import datetime, timezone

    from app.modules.channels.models_tenant import TiktokOrderRefreshQueue
    from app.modules.sales.models_tenant import Order
    from app.modules.tasks.models_tenant import WebhookEvent

    result = {"scanned": 0, "reconciled": 0, "requeued": 0, "skipped": 0, "mapping_missing": 0}
    rows = list(
        db.execute(
            select(WebhookEvent)
            .where(
                WebhookEvent.tenant_id == int(tenant_id),
                WebhookEvent.channel == CHANNEL_TIKTOK,
                WebhookEvent.status == "received",
            )
            .order_by(WebhookEvent.received_at.asc(), WebhookEvent.id.asc())
            .limit(max(1, min(int(limit), 200)))
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    result["scanned"] = len(rows)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for event in rows:
        payload = event.payload if isinstance(event.payload, dict) else {}
        order_ids = [str(item).strip() for item in (payload.get("order_ids") or []) if str(item or "").strip()]
        if not event.store_id:
            result["mapping_missing"] += 1
            continue
        if not order_ids:
            event.status = "ignored"
            event.processed_at = now
            result["skipped"] += 1
            continue
        existing = set(
            db.execute(
                select(Order.external_order_no).where(
                    Order.tenant_id == int(tenant_id),
                    Order.store_id == int(event.store_id),
                    Order.external_order_no.in_(order_ids),
                )
            ).scalars()
        )
        missing = [order_no for order_no in order_ids if order_no not in existing]
        if not missing:
            event.status = "processed"
            event.processed_at = now
            result["reconciled"] += 1
            continue
        for order_no in missing:
            queue_row = db.execute(
                select(TiktokOrderRefreshQueue).where(
                    TiktokOrderRefreshQueue.tenant_id == int(tenant_id),
                    TiktokOrderRefreshQueue.store_id == int(event.store_id),
                    TiktokOrderRefreshQueue.external_order_no == order_no,
                )
            ).scalar_one_or_none()
            if queue_row is not None and str(queue_row.status or "") in service.TIKTOK_REFRESH_STATUS_ACTIVE:
                result["skipped"] += 1
                continue
            service.enqueue_tiktok_order_refresh(
                db,
                int(tenant_id),
                int(event.store_id),
                order_no,
                reason="reconcile_received",
            )
            result["requeued"] += 1
    db.flush()
    return result


def reconcile_tiktok_webhook_events(tenant_id: int, limit: int = 50) -> dict[str, Any]:
    """历史 received 对账入口（调度进程调用，单租户异常自吞并返回）。"""
    try:
        with job_tenant_context(int(tenant_id)) as (context, db):
            summary = reconcile_tiktok_webhook_events_in_db(db, context.tenant_id, limit=limit)
            db.commit()
        return {"tenant_id": int(tenant_id), **summary}
    except Exception:  # noqa: BLE001 - 单租户失败不影响调度轮次
        logger.exception("TikTok webhook 历史对账失败：tenant=%s", tenant_id)
        return {"tenant_id": int(tenant_id), "scanned": 0, "reconciled": 0, "requeued": 0}


def tiktok_refresh_maintenance(tenant_id: int) -> dict[str, Any]:
    """调度进程每 15 秒调用：历史对账 + 到期扫描 + Celery 唤醒（只唤醒，不做业务处理）。"""
    reconciliation = reconcile_tiktok_webhook_events(int(tenant_id))
    wake = wake_tiktok_refresh(int(tenant_id))
    return {
        "reconciled": int(reconciliation.get("reconciled") or 0),
        "requeued": int(reconciliation.get("requeued") or 0),
        "due": int(wake.get("due") or 0),
        "woken": bool(wake.get("woken")),
    }


__all__ = [
    "claim_tiktok_refresh_batch",
    "compensate_tenant_webhook_events",
    "compensate_webhook_events",
    "confirm_tiktok_refresh_success",
    "dispatch",
    "fail_tiktok_refresh",
    "job_tenant_context",
    "process_tiktok_webhook_events",
    "push_stock",
    "push_stock_for_sku",
    "reconcile_tiktok_webhook_events",
    "refresh_expiring_tokens",
    "refresh_store_token",
    "register_outbox_routes",
    "run_order_sync",
    "run_stock_push",
    "scan_due_tiktok_refresh",
    "sync_store_listings",
    "tenant_owner_user_id",
    "tiktok_refresh_maintenance",
    "wake_tiktok_refresh",
]
