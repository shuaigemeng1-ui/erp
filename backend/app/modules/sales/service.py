"""销售服务：订单闭环、订单查询、库存推送入队。

跨模块固定契约（docs/architecture.md，签名不得修改）：

* ``enqueue_order_sync``：供渠道回调调用，返回 ``sync_run_id``。

订单闭环（plan.txt §4「商品与订单」「库存与任务可靠性」）：

1. 拉单拿到的原始数据**先**写 ``platform_order_raw`` 并提交，归一化失败可直接重试，
   不必重新拉单；
2. 归一化 ``orders`` / ``order_items``：以 (tenant, store, external_order_no) 唯一约束防重，
   并发插入用 SAVEPOINT 兜底；订单行加锁，同一订单串行处理；
3. 写 ``order_status_history``（只在状态真正变化时追加）；
4. 计算库存影响：目标量 = 扣减状态 ? 购买数量 : 0，本次差额 = 目标 - 已应用；
   通过 ``inventory.service.apply_balance_delta`` 写余额与流水，与
   ``order_stock_effects``、``outbox_events`` 同一租户库事务提交；外部已成立订单允许
   负库存但标记异常；重复拉单 / 回调 / 重试差额为 0，不会重复扣减或恢复；
5. 库存变化后在同一事务内写「库存推送」Outbox，事务提交后再请求平台。

跨模块调用一律函数内延迟 import：库存（catalog 负责）、渠道（channels 负责）并行开发时
即使尚未实现，也不会影响路由导入，任务会以清晰的错误信息失败并可重试。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.access import EffectivePermissions
from app.core.errors import AppError, ErrorCode, ScopeDeniedError
from app.db.base import utcnow
from app.modules.sales import repository
from app.modules.sales.models_tenant import (
    Order,
    OrderItem,
    OrderStatusHistory,
    OrderStockEffect,
    PlatformOrderRaw,
)
from app.modules.sales.order_mapping import (
    MAPPING_AMBIGUOUS,
    MAPPING_MAPPED,
    MAPPING_MISSING,
    NormalizedOrderStatus,
    ParsedOrder,
    mapping_status_for,
    merge_abnormal_reasons,
    parse_order_payload,
    push_quantity,
    compute_stock_delta,
    target_stock_quantity,
)
from app.modules.tasks import service as tasks_service
from app.modules.tasks.models_tenant import OutboxEvent, SyncRun

logger = logging.getLogger("ventasync.sales.service")

# 拉单默认回看窗口（天）
DEFAULT_SYNC_LOOKBACK_DAYS = 14
# 库存推送一次最多合并的 SKU 数量，超过则升级为整店推送
MAX_PUSH_SKU_IDS = 200
# 订单状态历史来源
SOURCE_SYNC = "sync"
SOURCE_WEBHOOK = "webhook"
SOURCE_MANUAL = "manual"


class DependencyUnavailable(RuntimeError):
    """跨模块依赖尚未就绪（并行开发期或部署顺序问题），任务可重试。"""


@dataclass(frozen=True, slots=True)
class StockPushItem:
    """库存推送批次中的一行：店铺平台商品 → 本地 SKU → 最新可售数量。"""

    listing_id: int
    store_id: int
    sku_id: int
    item_id: str
    model_id: str
    external_sku_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class OrderDetail:
    """订单详情聚合（明细 + 状态历史 + 库存影响）。"""

    order: Order
    items: tuple[OrderItem, ...]
    history: tuple[OrderStatusHistory, ...]
    effects: tuple[OrderStockEffect, ...]


# --------------------------------------------------------------------------- 固定契约
def enqueue_order_sync(
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    triggered_by: str = "webhook",
) -> int:
    """供渠道回调调用：登记订单同步任务并投递 Outbox，返回 ``sync_run_id``。

    * 同一店铺已有 pending 的订单同步任务时复用，避免回调风暴重复排队；
    * 真正防重由订单唯一约束与库存差额幂等保证，不依赖本函数去重。
    """
    if not store_id or store_id <= 0:
        raise AppError(ErrorCode.VALIDATION_ERROR, "同步订单必须指定店铺")
    return tasks_service.request_sync_run(
        db,
        tenant_id=tenant_id,
        job_type=tasks_service.JOB_ORDER_SYNC,
        store_id=store_id,
        triggered_by=triggered_by,
        requested_by=None,
    )


def request_manual_order_sync(
    db: Session,
    *,
    tenant_id: int,
    store_id: int | None,
    effective: EffectivePermissions,
    actor_id: int,
) -> int:
    """手动同步（202 接口）：先按数据范围校验，再建任务与审计。"""
    if store_id is not None:
        if not effective.covers("order.sync", store_id=store_id):
            raise ScopeDeniedError("无权同步该店铺的订单")
        store = _load_store(db, tenant_id=tenant_id, store_id=store_id)
        if store is None:
            raise AppError(ErrorCode.NOT_FOUND, "店铺不存在或不可见")
        if getattr(store, "status", "connected") != "connected":
            raise AppError(ErrorCode.STORE_NOT_CONNECTED)
    run_id = tasks_service.request_sync_run(
        db,
        tenant_id=tenant_id,
        job_type=tasks_service.JOB_ORDER_SYNC,
        store_id=store_id,
        triggered_by="manual",
        requested_by=actor_id,
    )
    tasks_service.record_audit(
        db,
        tenant_id=tenant_id,
        action="order.sync",
        resource_type="sync_run",
        resource_id=run_id,
        actor_id=actor_id,
        summary="手动同步订单",
        payload={"store_id": store_id},
    )
    return run_id


# --------------------------------------------------------------------------- 订单查询
def list_order_page(
    db: Session,
    *,
    tenant_id: int,
    effective: EffectivePermissions,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    store_id: int | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[Order], int]:
    """订单分页列表：按 ``order.read`` 的店铺数据范围过滤（空范围 = 看不到任何订单）。"""
    allowed = effective.allowed_ids("order.read", "stores")
    if store_id is not None:
        if allowed is not None and store_id not in allowed:
            raise ScopeDeniedError("无权查看该店铺的订单")
        store_ids: list[int] | None = [store_id]
    elif allowed is None:
        store_ids = None
    else:
        store_ids = sorted(allowed)
        if not store_ids:
            return [], 0
    return repository.list_orders(
        db,
        tenant_id,
        store_ids=store_ids,
        status=status,
        q=q,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )


def load_order_detail(
    db: Session,
    *,
    tenant_id: int,
    effective: EffectivePermissions,
    order_id: int,
) -> OrderDetail:
    """订单详情：对象不可见（不存在或不在数据范围内）统一返回 404。"""
    allowed = effective.allowed_ids("order.read", "stores")
    order = repository.get_order(db, tenant_id, order_id)
    if order is None or (allowed is not None and int(order.store_id) not in allowed):
        raise AppError(ErrorCode.ORDER_NOT_FOUND)
    return OrderDetail(
        order=order,
        items=tuple(repository.list_order_items(db, tenant_id, order_id)),
        history=tuple(repository.list_status_history(db, tenant_id, order_id)),
        effects=tuple(repository.list_stock_effects(db, tenant_id, order_id)),
    )


# --------------------------------------------------------------------------- 订单闭环入口
def ingest_platform_orders(
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    channel: str,
    payloads: list[dict[str, Any]],
    sync_run_id: int | None = None,
    triggered_by: str = "manual",
) -> dict[str, Any]:
    """把平台原始订单批量写入并归一化。幂等：重复调用不会重复建单 / 重复扣库存。

    返回 ``{"total","orders_created","orders_updated","stock_changed","errors","messages"}``，
    由任务层聚合到 ``sync_runs.total_processed``。
    """
    summary: dict[str, Any] = {
        "total": 0,
        "orders_created": 0,
        "orders_updated": 0,
        "stock_changed": 0,
        "errors": 0,
        "messages": [],
    }
    raw_ids: list[int] = []
    for payload in payloads or []:
        if not isinstance(payload, dict):
            summary["errors"] += 1
            summary["messages"].append("平台订单数据不是 JSON 对象")
            continue
        external_order_no = str(
            payload.get("order_sn") or payload.get("id") or payload.get("order_id") or ""
        ).strip()
        if not external_order_no:
            summary["errors"] += 1
            summary["messages"].append("平台订单数据缺少 order_sn")
            continue
        raw_id = _persist_platform_raw(
            db,
            tenant_id=tenant_id,
            store_id=store_id,
            channel=channel,
            external_order_no=external_order_no,
            payload=payload,
        )
        raw_ids.append(raw_id)
        summary["total"] += 1
    # 原始数据先落库：归一化即使失败，也可以只凭 platform_order_raw 重试
    db.commit()

    source = _status_source(triggered_by)
    for raw_id in raw_ids:
        try:
            result = normalize_order_from_raw(
                db,
                tenant_id=tenant_id,
                raw_id=raw_id,
                sync_run_id=sync_run_id,
                source=source,
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001 - 单笔失败不影响其它订单
            db.rollback()
            summary["errors"] += 1
            summary["messages"].append(f"订单归一化失败(raw_id={raw_id}): {exc}"[:300])
            logger.warning("订单归一化失败 tenant=%s raw=%s: %s", tenant_id, raw_id, exc)
            _enqueue_normalize_retry(db, tenant_id=tenant_id, raw_id=raw_id, sync_run_id=sync_run_id)
            continue
        if result["created"]:
            summary["orders_created"] += 1
        else:
            summary["orders_updated"] += 1
        if result["stock_changed"]:
            summary["stock_changed"] += 1
    return summary


def normalize_order_from_raw(
    db: Session,
    *,
    tenant_id: int,
    raw_id: int,
    sync_run_id: int | None = None,
    source: str = SOURCE_SYNC,
) -> dict[str, Any]:
    """把一条 ``platform_order_raw`` 归一化：订单 → 明细 → 状态历史 → 库存影响 → 推送事件。

    调用方负责提交事务；本函数内部只 flush，保证订单、库存影响、Outbox 原子提交。
    """
    raw = repository.get_platform_raw(db, tenant_id, raw_id)
    if raw is None:
        raise AppError(ErrorCode.NOT_FOUND, "平台原始订单不存在")
    parsed = parse_order_payload(raw.channel, dict(raw.payload or {}))
    if not parsed.external_order_no:
        raise ValueError("平台订单号为空")

    mapping = _resolve_sku_mapping(
        db,
        tenant_id=tenant_id,
        store_id=int(raw.store_id),
        keys=[(line.external_item_id, line.external_sku_id) for line in parsed.lines],
    )
    order, created, previous_status = _upsert_order(
        db,
        tenant_id=tenant_id,
        store_id=int(raw.store_id),
        channel=raw.channel,
        parsed=parsed,
        raw_id=int(raw.id),
    )
    items = _upsert_order_items(
        db,
        tenant_id=tenant_id,
        order=order,
        parsed=parsed,
        mapping=mapping,
    )
    _record_status_change(
        db,
        tenant_id=tenant_id,
        order=order,
        created=created,
        previous_status=previous_status,
        parsed=parsed,
        source=source,
    )

    # 无法识别的平台状态不猜库存：订单照常保存并标记异常，等状态明确后再结算
    if parsed.status == NormalizedOrderStatus.UNKNOWN:
        effect_result: dict[str, Any] = {
            "changed_skus": [],
            "negative_skus": [],
            "skipped_effect_ids": [int(item.id) for item in items],
        }
    else:
        effect_result = apply_order_stock_effects(
            db,
            tenant_id=tenant_id,
            order=order,
            items=items,
            status=parsed.status,
            sync_run_id=sync_run_id,
            source=source,
        )
    # 每次归一化都按当前状态重算异常标记：问题修复后异常应自动清除
    order.abnormal_reason = merge_abnormal_reasons(
        "",
        _abnormal_reasons(parsed=parsed, items=items, effect_result=effect_result),
    )
    db.flush()

    if effect_result["changed_skus"]:
        enqueue_stock_push(
            db,
            tenant_id=tenant_id,
            store_id=int(order.store_id),
            sku_ids=effect_result["changed_skus"],
            reason="order_stock_effect",
        )
    return {
        "order_id": int(order.id),
        "created": created,
        "status": str(order.status),
        "stock_changed": bool(effect_result["changed_skus"]),
        "abnormal_reason": order.abnormal_reason,
    }


def apply_order_stock_effects(
    db: Session,
    *,
    tenant_id: int,
    order: Order,
    items: list[OrderItem],
    status: str,
    sync_run_id: int | None = None,
    source: str = SOURCE_SYNC,
) -> dict[str, Any]:
    """按明细应用库存影响：本次差额 = 目标数量 - 已应用数量。

    * 固定加锁顺序：订单行（调用方已锁）→ 明细（排序后遍历）→ 库存影响 → 库存余额；
    * 差额为 0 不调用库存服务，重复拉单 / 回调 / 重试天然幂等；
    * 外部已成立订单允许负库存（``allow_negative=True``），但订单标记异常；
    * 与库存余额、库存流水、Outbox 同一事务提交（由调用方 commit）。
    """
    result: dict[str, Any] = {
        "changed_skus": [],
        "negative_skus": [],
        "skipped_effect_ids": [],
    }
    target_items = sorted(
        (item for item in items if item.sku_id),
        key=lambda item: (int(item.sku_id or 0), int(item.id)),
    )
    if not target_items:
        return result

    stock_place_id = resolve_default_stock_place(db, tenant_id)
    if stock_place_id is None:
        result["skipped_effect_ids"] = [int(item.id) for item in target_items]
        return result

    for item in target_items:
        target = target_stock_quantity(int(item.quantity or 0), status)
        effect = repository.get_stock_effect(
            db, tenant_id, int(item.id), stock_place_id
        )
        if effect is None:
            effect = OrderStockEffect(
                tenant_id=tenant_id,
                order_id=int(order.id),
                order_item_id=int(item.id),
                sku_id=int(item.sku_id),
                stock_place_id=stock_place_id,
                applied_quantity=0,
                target_quantity=target,
                status="pending",
                last_task_id=str(sync_run_id or ""),
            )
            db.add(effect)
            db.flush()
        delta = compute_stock_delta(target, int(effect.applied_quantity or 0))
        effect.target_quantity = target
        effect.last_task_id = str(sync_run_id or "")
        if delta == 0:
            effect.status = "applied" if target > 0 else "skipped"
            continue
        new_available = _apply_balance_delta(
            db,
            tenant_id=tenant_id,
            sku_id=int(item.sku_id),
            stock_place_id=stock_place_id,
            quantity_change=-delta,
            movement_type="order_deduct" if delta > 0 else "order_restore",
            reference_type="order",
            reference_id=int(order.id),
            operator_id=None,
            note=f"{source} 订单 {order.external_order_no} 库存{'扣减' if delta > 0 else '恢复'} {abs(delta)}",
            allow_negative=True,
        )
        effect.applied_quantity = target
        effect.status = "applied"
        if int(item.sku_id) not in result["changed_skus"]:
            result["changed_skus"].append(int(item.sku_id))
        if new_available is not None and new_available < 0 and int(item.sku_id) not in result["negative_skus"]:
            result["negative_skus"].append(int(item.sku_id))
    db.flush()
    return result


def retry_pending_stock_effects(
    db: Session, *, tenant_id: int, limit: int = 100
) -> dict[str, Any]:
    """重试未完成 / 失败的库存影响（Worker 中断后恢复未完成工作）。"""
    rows = db.execute(
        select(OrderStockEffect.order_id)
        .where(
            OrderStockEffect.tenant_id == tenant_id,
            OrderStockEffect.status.in_(["pending", "failed"]),
        )
        .group_by(OrderStockEffect.order_id)
        .limit(limit)
    ).scalars()
    processed = 0
    changed = 0
    for order_id in [int(item) for item in rows]:
        order = repository.lock_order(db, tenant_id, order_id)
        if order is None:
            continue
        items = repository.list_order_items(db, tenant_id, order_id)
        result = apply_order_stock_effects(
            db,
            tenant_id=tenant_id,
            order=order,
            items=items,
            status=str(order.status),
            sync_run_id=None,
            source=SOURCE_SYNC,
        )
        processed += 1
        if result["changed_skus"]:
            changed += 1
            enqueue_stock_push(
                db,
                tenant_id=tenant_id,
                store_id=int(order.store_id),
                sku_ids=result["changed_skus"],
                reason="stock_effect_retry",
            )
    db.flush()
    return {"orders": processed, "changed": changed}


# --------------------------------------------------------------------------- 库存推送
def stock_push_lock_name(
    tenant_id: int, store_id: int, sku_id: int | None = None
) -> str:
    """库存推送串行锁名（配合 ``app.core.redis.acquire_lock`` 使用）。

    锁只用于协调，最终防重仍然依赖数据库记录与业务幂等：推送任务执行时读取最新库存，
    因此旧任务不会把新数量覆盖回旧值。
    """
    if sku_id is not None:
        return f"stock_push:{tenant_id}:{store_id}:{sku_id}"
    return f"stock_push:{tenant_id}:{store_id}"


def enqueue_stock_push(
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    sku_ids: list[int] | None = None,
    reason: str = "",
) -> int:
    """登记库存推送事件，返回 Outbox 事件 ID。

    同一店铺已有待投递的推送事件时**合并**而不是新增（数据库记录级去重）：
    * 新事件是整店推送（``sku_ids=None``）或累计 SKU 超过上限 → 合并为整店推送；
    * 否则合并 SKU 集合。

    执行时任务会重新读取最新库存，因此合并与重复投递都不会造成旧值覆盖新值。
    """
    normalized: list[int] | None = None
    if sku_ids is not None:
        normalized = sorted({int(item) for item in sku_ids if int(item) > 0})
        if not normalized:
            normalized = None
    if normalized is not None and len(normalized) > MAX_PUSH_SKU_IDS:
        normalized = None

    existing = _pending_push_event(db, tenant_id, store_id)
    if existing is not None:
        payload = dict(existing.payload or {})
        existing_skus = payload.get("sku_ids")
        if existing_skus is None or normalized is None:
            payload["sku_ids"] = None
        else:
            merged = sorted({int(item) for item in existing_skus} | set(normalized))
            payload["sku_ids"] = None if len(merged) > MAX_PUSH_SKU_IDS else merged
        payload.update(
            {
                "tenant_id": int(tenant_id),
                "store_id": int(store_id),
                "reason": reason or str(payload.get("reason") or ""),
            }
        )
        existing.payload = payload
        db.flush()
        return int(existing.id)

    return tasks_service.enqueue_outbox(
        db,
        tenant_id=tenant_id,
        event_type=tasks_service.EVENT_STOCK_PUSH_REQUESTED,
        aggregate_type="store",
        aggregate_id=int(store_id),
        payload={
            "tenant_id": int(tenant_id),
            "store_id": int(store_id),
            "sku_ids": normalized,
            "reason": reason,
        },
    )


def _pending_push_event(
    db: Session, tenant_id: int, store_id: int
) -> OutboxEvent | None:
    """查询同一店铺待投递的库存推送事件（合并用）。"""
    from app.modules.tasks import repository as tasks_repository

    return tasks_repository.find_pending_outbox(
        db,
        tenant_id,
        tasks_service.EVENT_STOCK_PUSH_REQUESTED,
        "store",
        int(store_id),
    )


def build_stock_push_items(
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    sku_ids: list[int] | None = None,
) -> list[StockPushItem]:
    """构造库存推送批次：执行时读取**最新**库存，数量最低为 0。

    只推送已映射的平台商品；未映射 / 待处理的不猜数值。
    """
    listings = _list_mapped_listings(
        db, tenant_id=tenant_id, store_id=store_id, sku_ids=sku_ids
    )
    if not listings:
        return []
    quantities = _available_quantity_map(
        db, tenant_id=tenant_id, sku_ids=[int(row["sku_id"]) for row in listings]
    )
    items: list[StockPushItem] = []
    for row in listings:
        sku_id = int(row["sku_id"])
        items.append(
            StockPushItem(
                listing_id=int(row["listing_id"]),
                store_id=int(store_id),
                sku_id=sku_id,
                item_id=str(row["external_item_id"] or ""),
                model_id=str(row["external_sku_id"] or ""),
                external_sku_id=str(row["external_sku_id"] or ""),
                quantity=push_quantity(quantities.get(sku_id, 0)),
            )
        )
    return items


# --------------------------------------------------------------------------- 内部：原始数据
def _persist_platform_raw(
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    channel: str,
    external_order_no: str,
    payload: dict[str, Any],
) -> int:
    """写 / 更新平台原始订单（同店铺同订单号只保留最新一份），返回原始数据 ID。"""
    existing = repository.find_platform_raw(db, tenant_id, store_id, external_order_no)
    if existing is not None:
        existing.channel = channel
        existing.payload = payload
        existing.fetched_at = utcnow()
        db.flush()
        return int(existing.id)
    raw = PlatformOrderRaw(
        tenant_id=tenant_id,
        channel=channel,
        store_id=int(store_id),
        external_order_no=external_order_no,
        payload=payload,
        fetched_at=utcnow(),
    )
    try:
        with db.begin_nested():
            db.add(raw)
            db.flush()
    except IntegrityError:
        existing = repository.find_platform_raw(
            db, tenant_id, store_id, external_order_no
        )
        if existing is None:  # pragma: no cover - 唯一约束冲突后必然能查到
            raise
        existing.payload = payload
        existing.fetched_at = utcnow()
        db.flush()
        return int(existing.id)
    return int(raw.id)


def _enqueue_normalize_retry(
    db: Session, *, tenant_id: int, raw_id: int, sync_run_id: int | None
) -> None:
    """归一化失败后写延迟重试事件（原始数据已存在，重试不需要重新拉单）。"""
    try:
        tasks_service.enqueue_outbox(
            db,
            tenant_id=tenant_id,
            event_type=tasks_service.EVENT_ORDER_NORMALIZE,
            aggregate_type="platform_order_raw",
            aggregate_id=int(raw_id),
            payload={
                "tenant_id": int(tenant_id),
                "raw_id": int(raw_id),
                "sync_run_id": sync_run_id,
            },
            available_delay_seconds=60,
        )
        db.commit()
    except Exception:  # noqa: BLE001 - 重试事件写入失败不应掩盖原始错误
        db.rollback()
        logger.warning("写订单归一化重试事件失败 tenant=%s raw=%s", tenant_id, raw_id)


# --------------------------------------------------------------------------- 内部：归一化
def _upsert_order(
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    channel: str,
    parsed: ParsedOrder,
    raw_id: int,
) -> tuple[Order, bool, str]:
    """创建或更新订单，返回 ``(订单, 是否新建, 变更前状态)``；行锁保证同一订单串行。"""
    order = repository.find_order(db, tenant_id, store_id, parsed.external_order_no)
    created = False
    previous_status = ""
    if order is None:
        order = Order(
            tenant_id=tenant_id,
            store_id=int(store_id),
            channel=channel,
            external_order_no=parsed.external_order_no,
            status=str(parsed.status),
            platform_status=parsed.platform_status,
            currency=parsed.currency,
            total_amount=parsed.total_amount,
            buyer_name=parsed.buyer_name,
            placed_at=parsed.placed_at,
            raw_id=int(raw_id),
            abnormal_reason=parsed.abnormal_reason,
        )
        try:
            with db.begin_nested():
                db.add(order)
                db.flush()
            created = True
        except IntegrityError:
            order = repository.find_order(
                db, tenant_id, store_id, parsed.external_order_no
            )
            if order is None:  # pragma: no cover
                raise
    locked = repository.lock_order(db, tenant_id, int(order.id))
    if locked is not None:
        if not created:
            previous_status = str(locked.status)
        order = locked
    order.channel = channel
    order.platform_status = parsed.platform_status
    order.status = str(parsed.status)
    order.currency = parsed.currency or order.currency
    order.total_amount = parsed.total_amount
    order.buyer_name = parsed.buyer_name or order.buyer_name
    if parsed.placed_at is not None:
        order.placed_at = parsed.placed_at
    order.raw_id = int(raw_id)
    db.flush()
    return order, created, previous_status


def _upsert_order_items(
    db: Session,
    *,
    tenant_id: int,
    order: Order,
    parsed: ParsedOrder,
    mapping: dict[tuple[str, str], tuple[int | None, str]],
) -> list[OrderItem]:
    """创建或更新订单明细；SKU 映射状态按渠道映射结果写入。"""
    items: list[OrderItem] = []
    for line in parsed.lines:
        key = (line.external_item_id, line.external_sku_id)
        sku_id, link_status = mapping.get(key, (None, MAPPING_MISSING))
        mapping_status = mapping_status_for(sku_id) if sku_id else link_status
        item = repository.find_order_item(
            db,
            tenant_id,
            int(order.id),
            line.external_item_id,
            line.external_sku_id,
        )
        if item is None:
            item = OrderItem(
                tenant_id=tenant_id,
                order_id=int(order.id),
                external_item_id=line.external_item_id,
                external_sku_id=line.external_sku_id,
                sku_id=sku_id,
                item_name=line.item_name,
                quantity=max(0, int(line.quantity)),
                unit_price=line.unit_price,
                discount_amount=line.discount_amount,
                item_status=line.item_status,
                mapping_status=mapping_status,
            )
            try:
                with db.begin_nested():
                    db.add(item)
                    db.flush()
            except IntegrityError:
                item = repository.find_order_item(
                    db,
                    tenant_id,
                    int(order.id),
                    line.external_item_id,
                    line.external_sku_id,
                )
                if item is None:  # pragma: no cover
                    raise
        item.sku_id = sku_id
        item.item_name = line.item_name or item.item_name
        item.quantity = max(0, int(line.quantity))
        item.unit_price = line.unit_price
        item.discount_amount = line.discount_amount
        item.item_status = line.item_status
        item.mapping_status = mapping_status
        items.append(item)
    db.flush()
    return items


def _record_status_change(
    db: Session,
    *,
    tenant_id: int,
    order: Order,
    created: bool,
    previous_status: str,
    parsed: ParsedOrder,
    source: str,
) -> None:
    """只在状态真正变化（或新建订单）时追加一条状态历史。"""
    current = str(order.status)
    if not created and previous_status == current:
        return
    db.add(
        OrderStatusHistory(
            tenant_id=tenant_id,
            order_id=int(order.id),
            from_status="" if created else previous_status,
            to_status=current,
            platform_status=parsed.platform_status,
            source=source,
            changed_at=utcnow(),
        )
    )
    db.flush()


def _abnormal_reasons(
    *,
    parsed: ParsedOrder,
    items: list[OrderItem],
    effect_result: dict[str, Any],
) -> list[str]:
    """汇总订单异常：未知状态、未映射 SKU、负库存。"""
    reasons: list[str] = []
    if parsed.abnormal_reason:
        reasons.append(parsed.abnormal_reason)
    unmapped = [item for item in items if not item.sku_id]
    if unmapped:
        names = "、".join(
            (item.external_sku_id or item.external_item_id or "未知商品")[:32]
            for item in unmapped[:5]
        )
        reasons.append(f"{len(unmapped)} 条明细未映射 SKU（{names}），未扣减库存")
    if effect_result.get("negative_skus"):
        ids = "、".join(str(item) for item in effect_result["negative_skus"][:5])
        reasons.append(f"库存不足出现负库存（SKU {ids}），请核对")
    return reasons


def _status_source(triggered_by: str) -> str:
    if triggered_by in {"webhook", "webhook_retry"}:
        return SOURCE_WEBHOOK
    if triggered_by == "manual":
        return SOURCE_MANUAL
    return SOURCE_SYNC


def _resolve_sku_mapping(
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    keys: list[tuple[str, str]],
) -> dict[tuple[str, str], tuple[int | None, str]]:
    """解析平台商品 → 本地 SKU 映射（优先渠道服务，回落到文档化只读查询）。"""
    unique_keys = list(dict.fromkeys((str(a), str(b)) for a, b in keys))
    if not unique_keys:
        return {}
    try:
        from app.modules.channels.service import resolve_listings_sku as batch_resolver
    except (ImportError, AttributeError):
        batch_resolver = None
    if batch_resolver is not None:
        resolved = batch_resolver(db, tenant_id, store_id, unique_keys) or {}
        result: dict[tuple[str, str], tuple[int | None, str]] = {}
        for key, value in resolved.items():
            sku_id, _product_id = _unpack_mapping(value)
            # 渠道服务只返回已 mapped 的链接，未返回的按未映射处理
            result[(str(key[0]), str(key[1]))] = (sku_id, MAPPING_MISSING)
        for key in unique_keys:
            result.setdefault(key, (None, MAPPING_MISSING))
        return result
    return _resolve_sku_mapping_via_sql(
        db, tenant_id=tenant_id, store_id=store_id, keys=unique_keys
    )


def _unpack_mapping(value: Any) -> tuple[int | None, int | None]:
    """兼容渠道服务返回 ``(sku_id, product_id)`` 或 dict。"""
    if isinstance(value, dict):
        sku_id = value.get("sku_id")
        product_id = value.get("product_id")
    elif isinstance(value, (tuple, list)) and len(value) >= 2:
        sku_id, product_id = value[0], value[1]
    else:
        return None, None
    return (
        int(sku_id) if sku_id else None,
        int(product_id) if product_id else None,
    )


def _resolve_sku_mapping_via_sql(
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    keys: list[tuple[str, str]],
) -> dict[tuple[str, str], tuple[int | None, str]]:
    """只读查询渠道映射表（表与字段见 docs/data-model.md「店铺与平台商品」）。"""
    result: dict[tuple[str, str], tuple[int | None, str]] = {
        key: (None, MAPPING_MISSING) for key in keys
    }
    item_ids = sorted({key[0] for key in keys})
    if not item_ids:
        return result
    statement = text(
        """
        SELECT cl.external_item_id AS external_item_id,
               cl.external_sku_id AS external_sku_id,
               cll.sku_id AS sku_id,
               cll.link_status AS link_status
        FROM channel_listings AS cl
        JOIN channel_listing_links AS cll
          ON cll.listing_id = cl.id AND cll.tenant_id = cl.tenant_id
        WHERE cl.tenant_id = :tenant_id
          AND cl.store_id = :store_id
          AND cl.external_item_id IN :item_ids
        """
    ).bindparams(bindparam("item_ids", expanding=True))
    rows = db.execute(
        statement,
        {
            "tenant_id": int(tenant_id),
            "store_id": int(store_id),
            "item_ids": item_ids,
        },
    ).mappings()
    for row in rows:
        key = (str(row["external_item_id"] or ""), str(row["external_sku_id"] or ""))
        if key not in result:
            continue
        link_status = str(row["link_status"] or "")
        sku_id = int(row["sku_id"]) if row["sku_id"] else None
        if sku_id:
            result[key] = (sku_id, MAPPING_MAPPED)
        elif link_status == "ambiguous":
            result[key] = (None, MAPPING_AMBIGUOUS)
        else:
            result[key] = (None, MAPPING_MISSING)
    return result


# --------------------------------------------------------------------------- 内部：跨模块依赖
def _apply_balance_delta(db: Session, **kwargs: Any) -> int:
    """调用库存契约 ``inventory.service.apply_balance_delta``（延迟 import）。

    该函数由 catalog 负责人实现：行锁余额、固定顺序、写余额与流水同事务、
    返回调整后可用数量。未实现时抛出带说明的 ``DependencyUnavailable``，
    任务会以可重试失败结束，不会崩溃路由导入。
    """
    try:
        from app.modules.inventory.service import apply_balance_delta
    except ImportError as exc:  # pragma: no cover - 并行开发期
        raise DependencyUnavailable(
            "库存服务尚未就绪：inventory.service.apply_balance_delta 不可导入"
        ) from exc
    try:
        return int(apply_balance_delta(db, **kwargs))
    except NotImplementedError as exc:  # pragma: no cover - 并行开发期
        raise DependencyUnavailable(
            "库存服务尚未实现：inventory.service.apply_balance_delta 仍为占位实现"
        ) from exc


def _load_store(db: Session, *, tenant_id: int, store_id: int) -> Any | None:
    """读取店铺信息（延迟 import 渠道契约）。"""
    try:
        from app.modules.channels.service import load_store
    except ImportError:  # pragma: no cover - 并行开发期
        return _load_store_via_sql(db, tenant_id=tenant_id, store_id=store_id)
    try:
        return load_store(db, tenant_id, store_id)
    except NotImplementedError:  # pragma: no cover
        return _load_store_via_sql(db, tenant_id=tenant_id, store_id=store_id)


def _load_store_via_sql(db: Session, *, tenant_id: int, store_id: int) -> Any | None:
    """只读查询店铺（字段见 docs/data-model.md），返回简单命名空间对象。"""
    row = db.execute(
        text(
            """
            SELECT id, channel, name, status, region
            FROM stores
            WHERE tenant_id = :tenant_id AND id = :store_id
            """
        ),
        {"tenant_id": int(tenant_id), "store_id": int(store_id)},
    ).mappings().first()
    if row is None:
        return None
    return _StoreRow(
        id=int(row["id"]),
        channel=str(row["channel"] or ""),
        name=str(row["name"] or ""),
        status=str(row["status"] or ""),
        region=str(row.get("region") or ""),
    )


@dataclass(frozen=True, slots=True)
class _StoreRow:
    id: int
    channel: str
    name: str
    status: str
    region: str


def mark_store_synced(db: Session, *, tenant_id: int, store_id: int) -> None:
    """标记店铺同步时间（延迟 import 渠道契约，未实现时忽略）。"""
    try:
        from app.modules.channels.service import mark_store_synced as channel_mark
    except ImportError:  # pragma: no cover - 并行开发期
        return
    try:
        channel_mark(db, tenant_id, store_id)
    except NotImplementedError:  # pragma: no cover
        logger.debug("渠道模块尚未实现 mark_store_synced，跳过店铺同步时间更新")


def resolve_default_stock_place(db: Session, tenant_id: int) -> int | None:
    """默认库存地点：订单库存影响先统一作用在默认地点（无则退回第一个地点）。"""
    row = db.execute(
        text(
            """
            SELECT id FROM stock_places
            WHERE tenant_id = :tenant_id AND is_default = 1
            ORDER BY id ASC LIMIT 1
            """
        ),
        {"tenant_id": int(tenant_id)},
    ).first()
    if row is None:
        row = db.execute(
            text(
                "SELECT id FROM stock_places WHERE tenant_id = :tenant_id ORDER BY id ASC LIMIT 1"
            ),
            {"tenant_id": int(tenant_id)},
        ).first()
    return int(row[0]) if row is not None else None


def _list_mapped_listings(
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    sku_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """已映射的平台商品（只读渠道表，字段见 docs/data-model.md）。"""
    sql = """
        SELECT cl.id AS listing_id,
               cl.external_item_id AS external_item_id,
               cl.external_sku_id AS external_sku_id,
               cll.sku_id AS sku_id
        FROM channel_listings AS cl
        JOIN channel_listing_links AS cll
          ON cll.listing_id = cl.id AND cll.tenant_id = cl.tenant_id
        WHERE cl.tenant_id = :tenant_id
          AND cl.store_id = :store_id
          AND cll.link_status = 'mapped'
          AND cll.sku_id IS NOT NULL
    """
    params: dict[str, Any] = {"tenant_id": int(tenant_id), "store_id": int(store_id)}
    if sku_ids:
        sql += " AND cll.sku_id IN :sku_ids"
        params["sku_ids"] = [int(item) for item in sku_ids]
    statement = text(sql)
    if "sku_ids" in params:
        statement = statement.bindparams(bindparam("sku_ids", expanding=True))
    rows = db.execute(statement, params).mappings().all()
    return [dict(row) for row in rows]


def _available_quantity_map(
    db: Session, *, tenant_id: int, sku_ids: list[int]
) -> dict[int, int]:
    """各 SKU 的可用库存合计（只读库存余额表，字段见 docs/data-model.md）。"""
    unique_ids = sorted({int(item) for item in sku_ids})
    if not unique_ids:
        return {}
    statement = text(
        """
        SELECT sku_id, COALESCE(SUM(available_quantity), 0) AS available
        FROM inventory_balances
        WHERE tenant_id = :tenant_id AND sku_id IN :sku_ids
        GROUP BY sku_id
        """
    ).bindparams(bindparam("sku_ids", expanding=True))
    rows = db.execute(
        statement, {"tenant_id": int(tenant_id), "sku_ids": unique_ids}
    ).mappings()
    return {int(row["sku_id"]): int(row["available"] or 0) for row in rows}


def sync_since_default(db: Session, *, tenant_id: int, lookback_days: int = DEFAULT_SYNC_LOOKBACK_DAYS) -> datetime:
    """拉单起始时间：上次成功同步前 2 小时（重叠窗口保证不漏单），否则回看默认天数。"""
    last_finished = db.execute(
        select(func.max(SyncRun.finished_at)).where(
            SyncRun.tenant_id == tenant_id,
            SyncRun.job_type == tasks_service.JOB_ORDER_SYNC,
            SyncRun.status.in_(["succeeded", "partial"]),
        )
    ).scalar_one_or_none()
    now = utcnow()
    if isinstance(last_finished, datetime):
        since = last_finished - timedelta(hours=2)
        floor = now - timedelta(days=lookback_days)
        return since if since > floor else floor
    return now - timedelta(days=lookback_days)


def summarize_ingest(summary: dict[str, Any]) -> tuple[str, int, str]:
    """把 ingest 结果映射为任务状态 / 处理数 / 错误摘要（纯函数，便于测试）。"""
    total = int(summary.get("total") or 0)
    errors = int(summary.get("errors") or 0)
    processed = int(summary.get("orders_created", 0)) + int(summary.get("orders_updated", 0))
    messages = [str(item) for item in (summary.get("messages") or [])]
    if errors and processed == 0:
        return "failed", processed, "；".join(messages)[:1000] or f"{errors} 笔订单处理失败"
    if errors:
        return "partial", processed, "；".join(messages)[:1000]
    return "succeeded", max(processed, total), ""


__all__ = [
    "DependencyUnavailable",
    "OrderDetail",
    "StockPushItem",
    "apply_order_stock_effects",
    "build_stock_push_items",
    "enqueue_order_sync",
    "enqueue_stock_push",
    "ingest_platform_orders",
    "list_order_page",
    "load_order_detail",
    "mark_store_synced",
    "normalize_order_from_raw",
    "request_manual_order_sync",
    "resolve_default_stock_place",
    "retry_pending_stock_effects",
    "stock_push_lock_name",
    "summarize_ingest",
    "sync_since_default",
]
