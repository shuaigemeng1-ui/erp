"""Outbox 投递任务。

后台任务通过 Outbox 模式投递：业务事务内写 ``outbox_events``，独立 Worker 轮询后投递到
Celery 队列，避免「事务已提交但消息丢失 / 消息已发出但事务回滚」。

可靠性约定（见 plan.txt §4「库存与任务可靠性」）：

* 领取待投递事件使用 ``FOR UPDATE SKIP LOCKED``，多 Worker 并发领取互不阻塞；
* 投递成功标记 ``dispatched``；失败累加 ``attempts`` 并写 ``last_error``，
  按指数退避推迟 ``available_at``，超过上限标记 ``failed`` 由人工重试；
* 语义为「至少一次」投递，业务侧必须幂等（订单按唯一键防重、库存按差额幂等）；
* 队列消息只带 ID 与标识，不带明文凭据；真正的 Worker 任务会重建 TenantContext；
* 每类任务投递到独立队列（``default`` / ``sales`` / ``channel``），便于限流与扩容。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.db.base import utcnow
from app.db.session import center_session, tenant_session
from app.modules.tasks import repository
from app.modules.tasks.models_tenant import OutboxEvent
from app.modules.tasks.retry import (
    MAX_OUTBOX_ATTEMPTS,
    is_retryable_error,
    retry_delay_seconds,
    should_retry,
)
from app.modules.tasks.tenancy import active_tenant_ids, load_tenant_route

logger = logging.getLogger("ventasync.tasks.outbox")


@dataclass(frozen=True, slots=True)
class OutboxRoute:
    """事件到 Celery 任务的静态路由。"""

    task_name: str
    queue: str = "default"


# 默认路由表。新增事件类型时在这里登记（或在模块导入期调用 register_outbox_route）。
OUTBOX_ROUTES: dict[str, OutboxRoute] = {
    # 统一任务入口：按 sync_runs.job_type 再分发到具体业务处理器
    "sync_run.created": OutboxRoute("app.modules.tasks.tasks.execute_sync_run", "default"),
    # 单笔订单归一化重试（原始数据已落库，无需重新拉单）
    "order.normalize": OutboxRoute("app.modules.sales.tasks.normalize_order", "sales"),
    # 库存推送：执行时读取最新库存，按店铺 + SKU 串行（渠道任务内部加锁）
    "stock.push.requested": OutboxRoute("app.modules.channels.tasks.run_stock_push", "channel"),
}

_ROUTES_LOADED = False


def register_outbox_route(event_type: str, task_name: str, queue: str = "default") -> None:
    """登记 / 覆盖一条事件路由（供其它模块在导入期注册自己的事件）。"""
    value = (event_type or "").strip()
    if not value or not app_task_name_valid(task_name):
        raise AppError(ErrorCode.VALIDATION_ERROR, "事件路由登记参数不合法")
    OUTBOX_ROUTES[value] = OutboxRoute(task_name=task_name, queue=queue)


def app_task_name_valid(task_name: str) -> bool:
    """任务名必须是 ``a.b.c`` 形式，避免把任意字符串投递到队列。"""
    value = (task_name or "").strip()
    if not value or value.startswith(".") or value.endswith("."):
        return False
    return all(part.isidentifier() for part in value.split("."))


def load_optional_route_providers() -> None:
    """加载可选模块的事件路由（渠道模块可按需注册，缺失时忽略）。"""
    global _ROUTES_LOADED
    if _ROUTES_LOADED:
        return
    _ROUTES_LOADED = True
    try:
        from app.modules.channels import tasks as channel_tasks
    except Exception:  # noqa: BLE001 - 渠道模块并行开发期可能不存在
        logger.debug("渠道任务模块不可用，跳过事件路由注册")
        return
    register = getattr(channel_tasks, "register_outbox_routes", None)
    if callable(register):
        try:
            register(register_outbox_route)
        except Exception:  # noqa: BLE001 - 注册失败不影响默认路由
            logger.warning("渠道模块注册 Outbox 路由失败", exc_info=True)


def resolve_target(
    event_type: str,
    payload: dict[str, Any] | None,
    *,
    tenant_id: int | None = None,
    binding: Any | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """把事件解析为 ``(任务名, 队列, 关键字参数)``。

    * ``tenant_id`` 由调用方传入**数据库行**的租户（权威值），payload 只做一致性校验；
    * 库存推送按 payload 选择粒度：只有一个 SKU 时投递 ``push_stock_for_sku``，
      否则投递整店推送 ``push_stock``（渠道任务内部按店铺 + SKU 串行并读取最新库存）。
    """
    from app.modules.tasks import instance

    payload = payload or {}
    binding = binding if binding is not None else instance.current_binding()
    # 权威租户：数据库行优先；未提供时（纯函数单测 / 旧调用）才回落到 payload
    authoritative = (
        instance.authoritative_tenant(payload, int(tenant_id))
        if tenant_id is not None
        else int(payload.get("tenant_id") or 0)
    )
    if event_type == "stock.push.requested":
        store_id = int(payload.get("store_id") or 0)
        sku_ids = payload.get("sku_ids")
        if isinstance(sku_ids, list) and len(sku_ids) == 1:
            return (
                "app.modules.channels.tasks.push_stock_for_sku",
                "channel",
                {"tenant_id": authoritative, "store_id": store_id, "sku_id": int(sku_ids[0])},
            )
        return (
            "app.modules.channels.tasks.push_stock",
            "channel",
            {
                "tenant_id": authoritative,
                "store_id": store_id,
                "sku_ids": [int(item) for item in sku_ids] if isinstance(sku_ids, list) else None,
            },
        )

    route = OUTBOX_ROUTES.get(event_type)
    if route is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, f"未登记的 Outbox 事件类型: {event_type}")
    if event_type == "sync_run.created":
        from app.modules.tasks.tasks import JOB_QUEUES

        job_type = str(payload.get("job_type") or "")
        kwargs = {
            "sync_run_id": int(payload.get("sync_run_id") or 0),
            "tenant_id": authoritative,
            "job_type": job_type,
            "store_id": payload.get("store_id"),
            "triggered_by": str(payload.get("triggered_by") or "manual"),
        }
        return route.task_name, JOB_QUEUES.get(job_type, route.queue), kwargs
    kwargs = dict(payload)
    # 任何事件都不允许用 payload 覆盖权威租户
    kwargs["tenant_id"] = authoritative
    return route.task_name, route.queue, kwargs


def _celery_send(task_name: str, queue: str, kwargs: dict[str, Any]) -> None:
    """按任务名投递（不 import 其它模块，避免进程启动期的模块耦合）。"""
    from app.workers.celery_app import celery_app

    celery_app.send_task(task_name, kwargs=kwargs, queue=queue)


def _dispatch_one(
    event: OutboxEvent, *, binding: Any | None = None
) -> tuple[bool, str, BaseException | None]:
    """投递单条事件，返回 ``(是否成功, 错误信息, 异常对象)``；不改事件状态。"""
    from app.modules.tasks import instance

    binding = binding if binding is not None else instance.current_binding()
    try:
        # payload 只能与权威租户一致，禁止覆盖；否则视为伪造消息拒绝投递
        conflict = instance.payload_binding_conflict(
            event.payload, tenant_id=int(event.tenant_id), binding=binding
        )
        if conflict:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"Outbox 事件负载与实例/租户绑定冲突：{conflict}",
            )
        task_name, queue, kwargs = resolve_target(
            event.event_type, event.payload, tenant_id=int(event.tenant_id), binding=binding
        )
        if not app_task_name_valid(task_name):
            return False, f"非法任务名: {task_name}", None
        _celery_send(task_name, queue, kwargs)
        return True, "", None
    except Exception as exc:  # noqa: BLE001 - 投递异常统一记录并退避
        logger.warning("Outbox 事件 %s 投递失败: %s", event.id, exc)
        return False, f"{type(exc).__name__}: {exc}"[:1000], exc


def dispatch_tenant_batch(
    db: Any, tenant_id: int, *, limit: int, binding: Any | None = None
) -> dict[str, int]:
    """处理单个租户的一批待投递事件（调用方负责提交事务）。

    权威租户来自 ``outbox_events.tenant_id`` 列；payload 中的 tenant_id / instance_id
    只允许与权威值一致，否则视为伪造消息：事件置 failed、写审计、不投递。
    """
    from app.modules.tasks import instance

    binding = binding if binding is not None else instance.current_binding()
    now = utcnow()
    events = repository.pending_outbox_events(db, now=now, limit=limit, tenant_id=tenant_id)
    summary = {"scanned": len(events), "dispatched": 0, "failed": 0, "rejected": 0}
    for event in events:
        from app.modules.tasks import service as task_service
        payload = event.payload or {}
        job_type = (task_service.JOB_STOCK_PUSH if event.event_type == "stock.push.requested"
                    else task_service.JOB_ORDER_SYNC if event.event_type == "order.normalize"
                    else str(payload.get("job_type") or ""))
        if not task_service.job_enabled(db, tenant_id, job_type):
            event.available_at = now + timedelta(seconds=60)
            continue
        ok, error, exc = _dispatch_one(event, binding=binding)
        if ok:
            event.status = "dispatched"
            event.dispatched_at = utcnow()
            event.last_error = ""
            summary["dispatched"] += 1
            continue
        event.attempts = int(event.attempts or 0) + 1
        event.last_error = error
        # 负载与绑定冲突属于永久错误：直接置 failed，写审计，不再重试
        conflict = instance.payload_binding_conflict(
            payload, tenant_id=int(tenant_id), binding=binding
        )
        if conflict:
            event.status = "failed"
            summary["rejected"] += 1
            instance.audit_rejection(
                int(tenant_id),
                reason=conflict,
                action="task.outbox_rejected",
                summary=f"Outbox 事件被拒绝投递：{conflict}",
                extra={"event_id": int(event.id) if event.id else None, "event_type": event.event_type},
            )
            logger.error(
                "Outbox 事件 %s 负载与绑定冲突（%s），已置 failed 并写审计", event.id, conflict
            )
            continue
        retryable = exc is None or is_retryable_error(exc)
        if should_retry(event.attempts, MAX_OUTBOX_ATTEMPTS) and retryable:
            event.status = "pending"
            event.available_at = utcnow() + timedelta(
                seconds=retry_delay_seconds(event.attempts)
            )
        else:
            event.status = "failed"
        summary["failed"] += 1
    db.flush()
    return summary


def dispatch_pending(
    *,
    limit: int | None = None,
    tenant_ids: list[int] | None = None,
    binding: Any | None = None,
) -> dict[str, int]:
    """扫描本实例可调度的租户，投递待发送事件，返回汇总计数。

    已独立部署 / 迁移中的租户会被跳过（不再由旧中心 Worker 处理），计数写入
    ``skipped_tenants``，便于部署日志对账。
    """
    from app.modules.tasks import instance

    load_optional_route_providers()
    binding = binding if binding is not None else instance.current_binding()
    batch_limit = max(1, int(limit or settings.outbox_batch_size))
    candidates = tenant_ids if tenant_ids is not None else active_tenant_ids()
    allowed, skipped = instance.filter_dispatchable_tenants(
        [int(item) for item in candidates], binding=binding
    )
    total = {
        "scanned": 0,
        "dispatched": 0,
        "failed": 0,
        "rejected": 0,
        "tenants": 0,
        "skipped_tenants": len(skipped),
    }
    if skipped:
        logger.info(
            "Outbox 跳过 %s 个不可调度租户：%s",
            len(skipped),
            ", ".join(f"{item['tenant_id']}:{item.get('reason')}" for item in skipped[:10]),
        )
    for tenant_id in allowed:
        try:
            with center_session() as center:
                route = load_tenant_route(center, tenant_id)
            with tenant_session(route) as db:
                summary = dispatch_tenant_batch(
                    db, tenant_id, limit=batch_limit, binding=binding
                )
                db.commit()
        except Exception:  # noqa: BLE001 - 单租户失败不影响其它租户
            logger.exception("Outbox 扫描租户 %s 失败", tenant_id)
            continue
        total["tenants"] += 1
        for key in ("scanned", "dispatched", "failed", "rejected"):
            total[key] += summary[key]
    if total["scanned"] or total["skipped_tenants"]:
        logger.info(
            "Outbox 本轮: 租户=%s 跳过=%s 扫描=%s 投递=%s 失败=%s 拒绝=%s",
            total["tenants"],
            total["skipped_tenants"],
            total["scanned"],
            total["dispatched"],
            total["failed"],
            total["rejected"],
        )
    return total


@lru_cache(maxsize=1)
def _tick_seconds() -> int:
    return max(5, int(settings.outbox_dispatch_interval_seconds))


def main() -> int:
    """独立投递进程入口（部署脚本用 ``python -m app.workers.outbox`` 或直接调用）。

    与 API、业务 Worker 分进程运行；单次异常只记录不影响下一轮。
    """
    from app.core.logging import configure_logging

    configure_logging()
    logger.info("Outbox 投递进程启动，轮询间隔 %s 秒", _tick_seconds())
    try:
        while True:
            try:
                dispatch_pending()
            except KeyboardInterrupt:  # pragma: no cover - 进程信号
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Outbox 投递轮次失败")
            time.sleep(_tick_seconds())
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("Outbox 投递进程收到停止信号")
        return 0


# --------------------------------------------------------------------------- Celery 任务
def _celery_task(**options: Any) -> Any:
    """延迟创建 Celery 任务，避免本模块导入时依赖 Celery 运行环境。"""
    from app.workers.celery_app import celery_app

    return celery_app.task(**options)


@_celery_task(name="app.modules.tasks.outbox.dispatch_pending")
def dispatch_pending_task() -> dict[str, int]:
    """Celery 任务版入口：供定时调度（beat / worker）触发一次扫描。"""
    return dispatch_pending()


__all__ = [
    "OUTBOX_ROUTES",
    "OutboxRoute",
    "dispatch_pending",
    "dispatch_pending_task",
    "dispatch_tenant_batch",
    "load_optional_route_providers",
    "main",
    "register_outbox_route",
    "resolve_target",
]
