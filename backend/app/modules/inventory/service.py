"""库存业务服务（含供 sales 模块调用的固定契约）。

跨模块契约（docs/architecture.md「跨模块接口」签名固定，禁止修改）：

* ``ensure_stock_place(db, tenant_id, stock_place_id) -> None``
* ``apply_balance_delta(db, *, tenant_id, sku_id, stock_place_id, quantity_change,
  movement_type, reference_type="", reference_id=None, operator_id=None, note="",
  allow_negative=False) -> int``  返回调整后可用数量

并发与一致性（plan.txt §4「库存与任务可靠性」）：

1. 同一 SKU + 库存地点只对应一行余额；先锁库存地点行、再锁余额行（固定顺序）。
2. 余额更新与库存流水写入在同一事务内；``apply_balance_delta`` 不提交事务，由调用方
   （人工调整或 sales 订单处理）统一提交。
3. 人工调整 ``allow_negative=False`` 时不允许出现负库存，返回 409。
4. 外部已成立订单允许负库存（``allow_negative=True``），由 sales 标记异常。
5. 同一事务处理多个 SKU 时，调用方需按 ``(stock_place_id, sku_id)`` 升序调用，
   保证并发事务加锁顺序一致，避免死锁。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.access import EffectivePermissions
from app.core.errors import (
    AppError,
    ConflictError,
    ErrorCode,
    NotFoundError,
    ScopeDeniedError,
)
from app.core.permissions import SCOPE_STOCK_PLACES, Permission
from app.modules.catalog import repository as catalog_repo
from app.modules.inventory import repository as inventory_repo
from app.modules.inventory.models_tenant import InventoryBalance, StockMovement, StockPlace
from app.modules.inventory.schemas import (
    InventoryAdjustResult,
    InventoryBalanceOut,
    InventoryMovementOut,
    StockPlaceOut,
)


class MovementType(StrEnum):
    """库存流水类型（与 ``stock_movements.movement_type`` 取值对应）。"""

    MANUAL_ADJUST = "manual_adjust"
    ORDER_DEDUCT = "order_deduct"
    ORDER_RESTORE = "order_restore"
    INBOUND = "inbound"
    OUTBOUND = "outbound"


@dataclass(frozen=True, slots=True)
class AuditContext:
    """审计上下文：由 Router 从请求中提取，避免 Service 依赖 FastAPI Request。"""

    actor_id: int | None = None
    request_id: str = ""
    ip: str = ""


# --------------------------------------------------------------------------- 纯逻辑（可单测）
def available_after(current_available: int, quantity_change: int) -> int:
    """计算变动后的可用库存（不判断是否允许为负）。"""
    return int(current_available) + int(quantity_change)


def ensure_available_non_negative(available: int, *, allow_negative: bool) -> None:
    """人工调整禁止负库存；外部订单允许负库存并标记异常。"""
    if available < 0 and not allow_negative:
        raise AppError(
            ErrorCode.INVENTORY_NEGATIVE_FORBIDDEN,
            "人工调整不允许产生负库存",
            details={"available_quantity": int(available)},
        )


def scope_allowed_stock_places(
    effective: EffectivePermissions, permission: str = Permission.INVENTORY_READ
) -> frozenset[int] | None:
    """返回该权限在库存地点维度可见的 ID 集合；``None`` 表示全部，空集合表示无授权。"""
    return effective.allowed_ids(str(permission), SCOPE_STOCK_PLACES)


def ensure_stock_place_in_scope(
    effective: EffectivePermissions,
    permission: str,
    stock_place_id: int,
) -> None:
    """校验「同一角色绑定同时具备权限且覆盖该库存地点」。"""
    if not effective.covers(str(permission), stock_place_id=int(stock_place_id)):
        raise ScopeDeniedError("无权操作该库存地点")


# --------------------------------------------------------------------------- 固定契约（供 sales 调用）
def ensure_stock_place(db: Session, tenant_id: int, stock_place_id: int) -> None:
    """校验库存地点存在、属于当前租户且处于启用状态；不满足抛 404。"""
    place = inventory_repo.get_stock_place(
        db, tenant_id, int(stock_place_id), active_only=True
    )
    if place is None:
        raise NotFoundError("库存地点不存在或不可见")


def apply_balance_delta(
    db: Session,
    *,
    tenant_id: int,
    sku_id: int,
    stock_place_id: int,
    quantity_change: int,
    movement_type: str,
    reference_type: str = "",
    reference_id: int | None = None,
    operator_id: int | None = None,
    note: str = "",
    allow_negative: bool = False,
) -> int:
    """原子地修改库存余额并写库存流水，返回调整后可用数量。

    * 不提交事务：由调用方（人工调整 / sales 订单处理）统一 commit；
    * 先锁库存地点行、再锁余额行；余额不存在时在同一事务内创建初始行；
    * ``quantity_change == 0`` 时不写流水，直接返回当前可用数量。
    """
    change = int(quantity_change)
    sku = catalog_repo.get_sku(db, tenant_id, int(sku_id))
    if sku is None:
        raise AppError(ErrorCode.SKU_NOT_FOUND)

    place = inventory_repo.lock_stock_place(db, tenant_id, int(stock_place_id))
    if place is None:
        raise NotFoundError("库存地点不存在或不可见")

    if change == 0:
        balance = inventory_repo.get_balance(
            db, tenant_id, int(sku_id), int(stock_place_id)
        )
        return int(balance.available_quantity) if balance is not None else 0

    balance = inventory_repo.lock_balance(
        db, tenant_id, int(sku_id), int(stock_place_id)
    )
    if balance is None:
        # 库存地点行已加锁，同一地点内余额创建被串行化，唯一约束是最终防线
        balance = InventoryBalance(
            tenant_id=tenant_id,
            sku_id=int(sku_id),
            stock_place_id=int(stock_place_id),
            available_quantity=0,
            reserved_quantity=0,
            version=0,
        )
        db.add(balance)
        db.flush()

    current = int(balance.available_quantity)
    target = available_after(current, change)
    ensure_available_non_negative(target, allow_negative=allow_negative)

    balance.available_quantity = target
    balance.version = int(balance.version) + 1
    db.add(
        StockMovement(
            tenant_id=tenant_id,
            sku_id=int(sku_id),
            stock_place_id=int(stock_place_id),
            quantity_change=change,
            balance_after=target,
            # 字段长度与迁移一致，超长文本截断，避免外部平台长编号导致写入失败
            movement_type=str(movement_type)[:32],
            reference_type=str(reference_type or "")[:32],
            reference_id=int(reference_id) if reference_id is not None else None,
            operator_id=int(operator_id) if operator_id is not None else None,
            note=str(note or "")[:500],
        )
    )
    db.flush()
    return target


# --------------------------------------------------------------------------- 人工调整用例
def adjust_balance(
    db: Session,
    *,
    tenant_id: int,
    effective: EffectivePermissions,
    sku_id: int,
    stock_place_id: int,
    quantity_change: int,
    note: str = "",
    audit: AuditContext | None = None,
) -> InventoryAdjustResult:
    """人工调整库存：校验数据范围、禁止负库存、写流水与审计后提交事务。"""
    audit = audit or AuditContext()
    ensure_stock_place_in_scope(effective, Permission.INVENTORY_ADJUST, stock_place_id)

    sku = catalog_repo.get_sku(db, tenant_id, int(sku_id))
    if sku is None or sku.status != "active":
        raise AppError(ErrorCode.SKU_NOT_FOUND)

    try:
        available = apply_balance_delta(
            db,
            tenant_id=tenant_id,
            sku_id=int(sku_id),
            stock_place_id=int(stock_place_id),
            quantity_change=int(quantity_change),
            movement_type=MovementType.MANUAL_ADJUST,
            reference_type="manual",
            reference_id=None,
            operator_id=audit.actor_id,
            note=note,
            allow_negative=False,
        )
        balance = inventory_repo.get_balance(
            db, tenant_id, int(sku_id), int(stock_place_id)
        )
        _write_audit(
            db,
            tenant_id=tenant_id,
            action="inventory.adjust",
            resource_type="inventory_balance",
            resource_id=balance.id if balance is not None else None,
            audit=audit,
            summary=f"人工调整库存 {int(quantity_change):+d}",
            payload={
                "sku_id": str(int(sku_id)),
                "stock_place_id": str(int(stock_place_id)),
                "quantity_change": int(quantity_change),
                "available_quantity": available,
                "note": note,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("库存调整冲突，请重试") from exc

    return InventoryAdjustResult(
        balance_id=str(balance.id) if balance is not None else None,
        sku_id=str(int(sku_id)),
        stock_place_id=str(int(stock_place_id)),
        available_quantity=available,
    )


# --------------------------------------------------------------------------- 查询
def list_balances(
    db: Session,
    *,
    tenant_id: int,
    effective: EffectivePermissions,
    page: int,
    page_size: int,
    sku_id: int | None = None,
    stock_place_id: int | None = None,
    q: str | None = None,
) -> tuple[list[InventoryBalanceOut], int]:
    """库存余额分页列表；按 ``inventory.read`` 的库存地点数据范围过滤。"""
    allowed = scope_allowed_stock_places(effective, Permission.INVENTORY_READ)
    if allowed is not None and not allowed:
        return [], 0

    candidate_sku_ids: list[int] | None = None
    keyword = (q or "").strip()
    if keyword:
        candidate_sku_ids = catalog_repo.search_sku_ids(db, tenant_id, keyword)
        if not candidate_sku_ids:
            return [], 0

    balances, total = inventory_repo.list_balances(
        db,
        tenant_id=tenant_id,
        page=page,
        page_size=page_size,
        allowed_stock_place_ids=allowed,
        sku_id=sku_id,
        stock_place_id=stock_place_id,
        candidate_sku_ids=candidate_sku_ids,
    )
    sku_map, place_map = _load_display_maps(db, tenant_id, balances)
    items = [_balance_out(balance, sku_map, place_map) for balance in balances]
    return items, total


def list_movements(
    db: Session,
    *,
    tenant_id: int,
    effective: EffectivePermissions,
    page: int,
    page_size: int,
    sku_id: int | None = None,
    stock_place_id: int | None = None,
) -> tuple[list[InventoryMovementOut], int]:
    """库存流水分页列表；按 ``inventory.read`` 的库存地点数据范围过滤。"""
    allowed = scope_allowed_stock_places(effective, Permission.INVENTORY_READ)
    if allowed is not None and not allowed:
        return [], 0

    movements, total = inventory_repo.list_movements(
        db,
        tenant_id=tenant_id,
        page=page,
        page_size=page_size,
        allowed_stock_place_ids=allowed,
        sku_id=sku_id,
        stock_place_id=stock_place_id,
    )
    sku_ids = sorted({movement.sku_id for movement in movements})
    place_ids = sorted({movement.stock_place_id for movement in movements})
    sku_map = {
        item[0]: (item[1], item[2])
        for item in catalog_repo.list_sku_display(db, tenant_id, sku_ids)
    }
    place_map = {
        place.id: place
        for place in inventory_repo.list_stock_places_by_ids(db, tenant_id, place_ids)
    }
    items = [
        InventoryMovementOut(
            id=str(movement.id),
            sku_id=str(movement.sku_id),
            sku_code=sku_map.get(movement.sku_id, (None, None))[0],
            product_name=sku_map.get(movement.sku_id, (None, None))[1],
            stock_place_id=str(movement.stock_place_id),
            stock_place_name=place_map[movement.stock_place_id].name
            if movement.stock_place_id in place_map
            else None,
            quantity_change=movement.quantity_change,
            balance_after=movement.balance_after,
            movement_type=movement.movement_type,
            reference_type=movement.reference_type or "",
            reference_id=str(movement.reference_id)
            if movement.reference_id is not None
            else None,
            operator_id=str(movement.operator_id)
            if movement.operator_id is not None
            else None,
            note=movement.note or "",
            created_at=movement.created_at,
        )
        for movement in movements
    ]
    return items, total


def list_stock_places(
    db: Session,
    *,
    tenant_id: int,
    effective: EffectivePermissions,
) -> list[StockPlaceOut]:
    """库存地点列表；按 ``inventory.read`` 的库存地点数据范围过滤。"""
    allowed = scope_allowed_stock_places(effective, Permission.INVENTORY_READ)
    places = inventory_repo.list_stock_places(
        db, tenant_id, allowed_ids=allowed, active_only=True
    )
    return [_stock_place_out(place) for place in places]


# --------------------------------------------------------------------------- 内部实现
def _load_display_maps(
    db: Session, tenant_id: int, balances: list[InventoryBalance]
) -> tuple[dict[int, tuple[str, str | None]], dict[int, StockPlace]]:
    sku_ids = sorted({balance.sku_id for balance in balances})
    place_ids = sorted({balance.stock_place_id for balance in balances})
    sku_map: dict[int, tuple[str, str | None]] = {}
    for sku_id, sku_code, product_name in catalog_repo.list_sku_display(
        db, tenant_id, sku_ids
    ):
        sku_map[sku_id] = (sku_code, product_name)
    place_map = {
        place.id: place
        for place in inventory_repo.list_stock_places_by_ids(db, tenant_id, place_ids)
    }
    return sku_map, place_map


def _balance_out(
    balance: InventoryBalance,
    sku_map: dict[int, tuple[str, str | None]],
    place_map: dict[int, StockPlace],
) -> InventoryBalanceOut:
    sku_code, product_name = sku_map.get(balance.sku_id, (None, None))
    place = place_map.get(balance.stock_place_id)
    return InventoryBalanceOut(
        id=str(balance.id),
        sku_id=str(balance.sku_id),
        sku_code=sku_code,
        product_name=product_name,
        stock_place_id=str(balance.stock_place_id),
        stock_place_name=place.name if place is not None else None,
        available_quantity=int(balance.available_quantity),
        reserved_quantity=int(balance.reserved_quantity),
        version=int(balance.version),
        updated_at=balance.updated_at,
    )


def _stock_place_out(place: StockPlace) -> StockPlaceOut:
    return StockPlaceOut(
        id=str(place.id),
        code=place.code,
        name=place.name,
        is_default=bool(place.is_default),
        status=place.status,
    )


def _write_audit(
    db: Session,
    *,
    tenant_id: int,
    action: str,
    resource_type: str,
    resource_id: int | None,
    audit: AuditContext,
    summary: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """写租户审计（与库存调整同事务）。延迟导入避免并行开发期循环依赖。"""
    from app.modules.audit.service import record

    record(
        db,
        tenant_id=tenant_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        actor_id=audit.actor_id,
        summary=summary,
        payload=payload,
        request_id=audit.request_id,
        ip=audit.ip,
    )


__all__ = [
    "MovementType",
    "AuditContext",
    "available_after",
    "ensure_available_non_negative",
    "scope_allowed_stock_places",
    "ensure_stock_place_in_scope",
    "ensure_stock_place",
    "apply_balance_delta",
    "adjust_balance",
    "list_balances",
    "list_movements",
    "list_stock_places",
]
