"""销售（订单）模块仓储层：只做 SQLAlchemy 查询，不写业务规则、不开事务。

所有查询都带 ``tenant_id`` 条件；列表与总数使用同一过滤条件（数据范围由 Service 传入）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.modules.sales.models_tenant import (
    Order,
    OrderItem,
    OrderStatusHistory,
    OrderStockEffect,
    PlatformOrderRaw,
)


def _apply_order_filters(
    stmt: Select[Any],
    tenant_id: int,
    *,
    store_ids: list[int] | None,
    status: str | None,
    q: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> Select[Any]:
    stmt = stmt.where(Order.tenant_id == tenant_id)
    if store_ids is not None:
        stmt = stmt.where(Order.store_id.in_(store_ids))
    if status:
        stmt = stmt.where(Order.status == status)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Order.external_order_no.like(pattern),
                Order.buyer_name.like(pattern),
            )
        )
    if date_from is not None:
        stmt = stmt.where(Order.placed_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Order.placed_at <= date_to)
    return stmt


def list_orders(
    db: Session,
    tenant_id: int,
    *,
    store_ids: list[int] | None = None,
    status: str | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Order], int]:
    base = _apply_order_filters(
        select(Order),
        tenant_id,
        store_ids=store_ids,
        status=status,
        q=q,
        date_from=date_from,
        date_to=date_to,
    )
    total = int(
        db.execute(select(func.count()).select_from(base.subquery())).scalar_one() or 0
    )
    rows = (
        db.execute(
            base.order_by(Order.placed_at.desc(), Order.id.desc())
            .offset(max(0, (page - 1) * page_size))
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return list(rows), total


def get_order(db: Session, tenant_id: int, order_id: int) -> Order | None:
    return db.execute(
        select(Order).where(Order.tenant_id == tenant_id, Order.id == order_id)
    ).scalar_one_or_none()


def find_order(
    db: Session, tenant_id: int, store_id: int, external_order_no: str
) -> Order | None:
    return db.execute(
        select(Order).where(
            Order.tenant_id == tenant_id,
            Order.store_id == store_id,
            Order.external_order_no == external_order_no,
        )
    ).scalar_one_or_none()


def lock_order(db: Session, tenant_id: int, order_id: int) -> Order | None:
    """行锁订单：同一订单的并发归一化 / 回调串行执行（固定加锁顺序的第一层）。"""
    return db.execute(
        select(Order)
        .where(Order.tenant_id == tenant_id, Order.id == order_id)
        .with_for_update()
    ).scalar_one_or_none()


def list_order_items(db: Session, tenant_id: int, order_id: int) -> list[OrderItem]:
    return list(
        db.execute(
            select(OrderItem)
            .where(OrderItem.tenant_id == tenant_id, OrderItem.order_id == order_id)
            .order_by(OrderItem.id.asc())
        )
        .scalars()
        .all()
    )


def find_order_item(
    db: Session,
    tenant_id: int,
    order_id: int,
    external_item_id: str,
    external_sku_id: str,
) -> OrderItem | None:
    return db.execute(
        select(OrderItem).where(
            OrderItem.tenant_id == tenant_id,
            OrderItem.order_id == order_id,
            OrderItem.external_item_id == external_item_id,
            OrderItem.external_sku_id == external_sku_id,
        )
    ).scalar_one_or_none()


def list_status_history(
    db: Session, tenant_id: int, order_id: int
) -> list[OrderStatusHistory]:
    return list(
        db.execute(
            select(OrderStatusHistory)
            .where(
                OrderStatusHistory.tenant_id == tenant_id,
                OrderStatusHistory.order_id == order_id,
            )
            .order_by(OrderStatusHistory.id.asc())
        )
        .scalars()
        .all()
    )


def list_stock_effects(
    db: Session, tenant_id: int, order_id: int
) -> list[OrderStockEffect]:
    return list(
        db.execute(
            select(OrderStockEffect)
            .where(
                OrderStockEffect.tenant_id == tenant_id,
                OrderStockEffect.order_id == order_id,
            )
            .order_by(OrderStockEffect.id.asc())
        )
        .scalars()
        .all()
    )


def get_stock_effect(
    db: Session, tenant_id: int, order_item_id: int, stock_place_id: int
) -> OrderStockEffect | None:
    return db.execute(
        select(OrderStockEffect)
        .where(
            OrderStockEffect.tenant_id == tenant_id,
            OrderStockEffect.order_item_id == order_item_id,
            OrderStockEffect.stock_place_id == stock_place_id,
        )
        .with_for_update()
    ).scalar_one_or_none()


def get_platform_raw(db: Session, tenant_id: int, raw_id: int) -> PlatformOrderRaw | None:
    return db.execute(
        select(PlatformOrderRaw).where(
            PlatformOrderRaw.tenant_id == tenant_id, PlatformOrderRaw.id == raw_id
        )
    ).scalar_one_or_none()


def find_platform_raw(
    db: Session, tenant_id: int, store_id: int, external_order_no: str
) -> PlatformOrderRaw | None:
    return db.execute(
        select(PlatformOrderRaw).where(
            PlatformOrderRaw.tenant_id == tenant_id,
            PlatformOrderRaw.store_id == store_id,
            PlatformOrderRaw.external_order_no == external_order_no,
        )
    ).scalar_one_or_none()
