"""订单路由（租户库）。

* ``GET  /tenants/{tenant_id}/orders``                 订单列表（按店铺数据范围过滤）
* ``GET  /tenants/{tenant_id}/orders/{order_id}``     订单详情（明细 / 状态历史 / 库存影响）
* ``POST /tenants/{tenant_id}/orders/sync``           手动触发拉单（202 + task_id）

权限：列表 / 详情需 ``order.read``，同步需 ``order.sync``；数据范围在 Service 层校验，
对象不可见统一返回 404（不泄漏其它店铺订单是否存在）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Path, Query

from app.api.deps import AccessSnapshotDep, TenantCtx, TenantDb, require_permission
from app.core.http import PageParams, accepted, paged, success
from app.core.permissions import Permission
from app.modules.sales import service as sales_service
from app.modules.sales.schemas import (
    OrderDetailOut,
    OrderOut,
    OrderStatusHistoryOut,
    OrderStockEffectOut,
    OrderItemOut,
    OrderSyncRequest,
)

router = APIRouter(prefix="/tenants/{tenant_id}/orders", tags=["orders"])

_ORDER_READ = Depends(require_permission(Permission.ORDER_READ))
_ORDER_SYNC = Depends(require_permission(Permission.ORDER_SYNC))


@router.get("", summary="订单列表")
def list_orders(
    tenant_ctx: TenantCtx,
    db: TenantDb,
    snapshot: AccessSnapshotDep,
    pagination: PageParams = Depends(),
    status: str | None = Query(default=None, max_length=32, description="归一化订单状态"),
    store_id: int | None = Query(default=None, ge=1, description="店铺ID"),
    q: str | None = Query(default=None, max_length=100, description="订单号 / 买家模糊搜索"),
    date_from: datetime | None = Query(default=None, description="下单时间起（UTC）"),
    date_to: datetime | None = Query(default=None, description="下单时间止（UTC）"),
    _permission: Any = _ORDER_READ,
) -> dict[str, Any]:
    items, total = sales_service.list_order_page(
        db,
        tenant_id=tenant_ctx.tenant_id,
        effective=snapshot.effective,
        page=pagination.page,
        page_size=pagination.page_size,
        status=status,
        store_id=store_id,
        q=q,
        date_from=date_from,
        date_to=date_to,
    )
    return paged(
        [OrderOut.from_model(order) for order in items],
        total,
        pagination.page,
        pagination.page_size,
    )


@router.get("/{order_id}", summary="订单详情")
def get_order(
    tenant_ctx: TenantCtx,
    db: TenantDb,
    snapshot: AccessSnapshotDep,
    order_id: int = Path(ge=1, description="订单ID"),
    _permission: Any = _ORDER_READ,
) -> dict[str, Any]:
    detail = sales_service.load_order_detail(
        db,
        tenant_id=tenant_ctx.tenant_id,
        effective=snapshot.effective,
        order_id=order_id,
    )
    payload = OrderDetailOut(
        **OrderOut.from_model(detail.order).model_dump(),
        items=[OrderItemOut.from_model(item) for item in detail.items],
        history=[OrderStatusHistoryOut.from_model(row) for row in detail.history],
        stock_effects=[OrderStockEffectOut.from_model(row) for row in detail.effects],
    )
    return success(payload)


@router.post("/sync", status_code=202, summary="手动同步订单（异步）")
def sync_orders(
    tenant_ctx: TenantCtx,
    db: TenantDb,
    snapshot: AccessSnapshotDep,
    payload: OrderSyncRequest | None = None,
    _permission: Any = _ORDER_SYNC,
) -> Any:
    run_id = sales_service.request_manual_order_sync(
        db,
        tenant_id=tenant_ctx.tenant_id,
        store_id=payload.store_id if payload else None,
        effective=snapshot.effective,
        actor_id=tenant_ctx.user_id,
    )
    db.commit()
    return accepted({"task_id": str(run_id)})
