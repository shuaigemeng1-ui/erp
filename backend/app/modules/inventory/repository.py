"""库存数据访问层：只做 SQLAlchemy 查询，不写业务规则、不提交事务。

加锁约定（见 plan.txt §4「库存与任务可靠性」）：

* 所有库存写入必须先锁「库存地点行」，再锁「余额行」，固定这个顺序避免死锁；
* 调用方在同一事务内处理多个 SKU 时，需按 ``(stock_place_id, sku_id)`` 升序调用，
  保证多事务之间加锁顺序一致；
* 余额行用 ``SELECT ... FOR UPDATE`` 加排他锁，并配合 ``version`` 递增记录变更。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.inventory.models_tenant import (
    InventoryBalance,
    StockMovement,
    StockPlace,
)


def get_stock_place(
    db: Session,
    tenant_id: int,
    stock_place_id: int,
    *,
    active_only: bool = True,
) -> StockPlace | None:
    stmt = select(StockPlace).where(
        StockPlace.tenant_id == tenant_id, StockPlace.id == stock_place_id
    )
    if active_only:
        stmt = stmt.where(StockPlace.status == "active")
    return db.execute(stmt).scalar_one_or_none()


def lock_stock_place(
    db: Session, tenant_id: int, stock_place_id: int
) -> StockPlace | None:
    """锁定库存地点行（第一把锁）；不存在或非启用状态返回 ``None``。"""
    stmt = (
        select(StockPlace)
        .where(
            StockPlace.tenant_id == tenant_id,
            StockPlace.id == stock_place_id,
            StockPlace.status == "active",
        )
        .with_for_update()
    )
    return db.execute(stmt).scalar_one_or_none()


def get_balance(
    db: Session, tenant_id: int, sku_id: int, stock_place_id: int
) -> InventoryBalance | None:
    stmt = select(InventoryBalance).where(
        InventoryBalance.tenant_id == tenant_id,
        InventoryBalance.sku_id == sku_id,
        InventoryBalance.stock_place_id == stock_place_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def lock_balance(
    db: Session, tenant_id: int, sku_id: int, stock_place_id: int
) -> InventoryBalance | None:
    """锁定余额行（第二把锁），用于并发调整不丢数量。"""
    stmt = (
        select(InventoryBalance)
        .where(
            InventoryBalance.tenant_id == tenant_id,
            InventoryBalance.sku_id == sku_id,
            InventoryBalance.stock_place_id == stock_place_id,
        )
        .with_for_update()
    )
    return db.execute(stmt).scalar_one_or_none()


def list_stock_places(
    db: Session,
    tenant_id: int,
    *,
    allowed_ids: frozenset[int] | None = None,
    active_only: bool = True,
) -> list[StockPlace]:
    """库存地点列表；``allowed_ids`` 为 ``None`` 表示全部，空集合表示无授权。"""
    if allowed_ids is not None and not allowed_ids:
        return []
    stmt = select(StockPlace).where(StockPlace.tenant_id == tenant_id)
    if active_only:
        stmt = stmt.where(StockPlace.status == "active")
    if allowed_ids is not None:
        stmt = stmt.where(StockPlace.id.in_(sorted(allowed_ids)))
    stmt = stmt.order_by(StockPlace.is_default.desc(), StockPlace.id)
    return list(db.execute(stmt).scalars().all())


def list_stock_places_by_ids(
    db: Session, tenant_id: int, stock_place_ids: Sequence[int]
) -> list[StockPlace]:
    if not stock_place_ids:
        return []
    stmt = select(StockPlace).where(
        StockPlace.tenant_id == tenant_id,
        StockPlace.id.in_(list(stock_place_ids)),
    )
    return list(db.execute(stmt).scalars().all())


def list_balances(
    db: Session,
    *,
    tenant_id: int,
    page: int,
    page_size: int,
    allowed_stock_place_ids: frozenset[int] | None = None,
    sku_id: int | None = None,
    stock_place_id: int | None = None,
    candidate_sku_ids: Sequence[int] | None = None,
) -> tuple[list[InventoryBalance], int]:
    """分页查询余额；``candidate_sku_ids`` 用于关键字搜索命中的 SKU 过滤。"""
    if candidate_sku_ids is not None and not candidate_sku_ids:
        return [], 0
    conditions = [InventoryBalance.tenant_id == tenant_id]
    if allowed_stock_place_ids is not None:
        if not allowed_stock_place_ids:
            return [], 0
        conditions.append(
            InventoryBalance.stock_place_id.in_(sorted(allowed_stock_place_ids))
        )
    if sku_id is not None:
        conditions.append(InventoryBalance.sku_id == sku_id)
    if stock_place_id is not None:
        conditions.append(InventoryBalance.stock_place_id == stock_place_id)
    if candidate_sku_ids is not None:
        conditions.append(InventoryBalance.sku_id.in_(list(candidate_sku_ids)))

    total = int(
        db.execute(
            select(func.count()).select_from(InventoryBalance).where(*conditions)
        ).scalar_one()
    )
    stmt = (
        select(InventoryBalance)
        .where(*conditions)
        .order_by(InventoryBalance.updated_at.desc(), InventoryBalance.id.desc())
        .offset(max(page - 1, 0) * page_size)
        .limit(page_size)
    )
    return list(db.execute(stmt).scalars().all()), total


def list_movements(
    db: Session,
    *,
    tenant_id: int,
    page: int,
    page_size: int,
    allowed_stock_place_ids: frozenset[int] | None = None,
    sku_id: int | None = None,
    stock_place_id: int | None = None,
) -> tuple[list[StockMovement], int]:
    if allowed_stock_place_ids is not None and not allowed_stock_place_ids:
        return [], 0
    conditions = [StockMovement.tenant_id == tenant_id]
    if allowed_stock_place_ids is not None:
        conditions.append(
            StockMovement.stock_place_id.in_(sorted(allowed_stock_place_ids))
        )
    if sku_id is not None:
        conditions.append(StockMovement.sku_id == sku_id)
    if stock_place_id is not None:
        conditions.append(StockMovement.stock_place_id == stock_place_id)

    total = int(
        db.execute(
            select(func.count()).select_from(StockMovement).where(*conditions)
        ).scalar_one()
    )
    stmt = (
        select(StockMovement)
        .where(*conditions)
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .offset(max(page - 1, 0) * page_size)
        .limit(page_size)
    )
    return list(db.execute(stmt).scalars().all()), total


__all__ = [
    "get_stock_place",
    "lock_stock_place",
    "get_balance",
    "lock_balance",
    "list_stock_places",
    "list_stock_places_by_ids",
    "list_balances",
    "list_movements",
]
