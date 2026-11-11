"""库存路由（租户库）。

接口族（见 docs/api-contract.md「商品与库存」）：

* ``GET  /tenants/{tenant_id}/inventory``              库存余额列表（按库存地点数据范围过滤）
* ``POST /tenants/{tenant_id}/inventory/adjustments``  人工调整（禁止负库存，409）
* ``GET  /tenants/{tenant_id}/inventory/movements``    库存流水
* ``GET  /tenants/{tenant_id}/inventory/stock-places`` 库存地点列表

路由层只做参数校验、权限依赖与响应包装；业务规则和事务在 ``service``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import (
    TenantCtx,
    TenantDb,
    require_permission,
)
from app.core.context import TenantContext, get_request_id
from app.core.http import PageParams, paged, success
from app.core.permissions import Permission
from app.modules.access.service import AccessSnapshot
from app.modules.inventory import service as inventory_service
from app.modules.inventory.schemas import InventoryAdjustRequest

router = APIRouter(prefix="/tenants/{tenant_id}/inventory", tags=["inventory"])

ReadAccess = Annotated[
    AccessSnapshot, Depends(require_permission(Permission.INVENTORY_READ))
]
AdjustAccess = Annotated[
    AccessSnapshot, Depends(require_permission(Permission.INVENTORY_ADJUST))
]


def _audit_context(request: Request, ctx: TenantContext) -> inventory_service.AuditContext:
    client_ip = request.client.host if request.client is not None else ""
    return inventory_service.AuditContext(
        actor_id=ctx.actor_id or ctx.user_id, request_id=get_request_id(), ip=client_ip
    )


@router.get("", summary="库存余额列表")
def list_balances(
    ctx: TenantCtx,
    db: TenantDb,
    snapshot: ReadAccess,
    page: Annotated[PageParams, Depends()],
    sku_id: int | None = Query(default=None, gt=0, description="按 SKU 过滤"),
    stock_place_id: int | None = Query(default=None, gt=0, description="按库存地点过滤"),
    q: str | None = Query(default=None, max_length=100, description="按 SKU 编码 / 商品名称搜索"),
) -> dict[str, Any]:
    items, total = inventory_service.list_balances(
        db,
        tenant_id=ctx.tenant_id,
        effective=snapshot.effective,
        page=page.page,
        page_size=page.page_size,
        sku_id=sku_id,
        stock_place_id=stock_place_id,
        q=q,
    )
    return paged(
        [item.model_dump() for item in items], total, page.page, page.page_size
    )


@router.post("/adjustments", summary="人工调整库存")
def adjust_balance(
    ctx: TenantCtx,
    db: TenantDb,
    snapshot: AdjustAccess,
    payload: InventoryAdjustRequest,
    request: Request,
) -> dict[str, Any]:
    result = inventory_service.adjust_balance(
        db,
        tenant_id=ctx.tenant_id,
        effective=snapshot.effective,
        sku_id=payload.sku_id,
        stock_place_id=payload.stock_place_id,
        quantity_change=payload.quantity_change,
        note=payload.note,
        audit=_audit_context(request, ctx),
    )
    return success(result.model_dump())


@router.get("/movements", summary="库存流水")
def list_movements(
    ctx: TenantCtx,
    db: TenantDb,
    snapshot: ReadAccess,
    page: Annotated[PageParams, Depends()],
    sku_id: int | None = Query(default=None, gt=0, description="按 SKU 过滤"),
    stock_place_id: int | None = Query(default=None, gt=0, description="按库存地点过滤"),
) -> dict[str, Any]:
    items, total = inventory_service.list_movements(
        db,
        tenant_id=ctx.tenant_id,
        effective=snapshot.effective,
        page=page.page,
        page_size=page.page_size,
        sku_id=sku_id,
        stock_place_id=stock_place_id,
    )
    return paged(
        [item.model_dump() for item in items], total, page.page, page.page_size
    )


@router.get("/stock-places", summary="库存地点列表")
def list_stock_places(
    ctx: TenantCtx,
    db: TenantDb,
    snapshot: ReadAccess,
) -> dict[str, Any]:
    items = inventory_service.list_stock_places(
        db, tenant_id=ctx.tenant_id, effective=snapshot.effective
    )
    return success([item.model_dump() for item in items])


__all__ = ["router"]
