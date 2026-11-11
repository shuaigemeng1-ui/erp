"""商品路由（租户库）。

接口族（见 docs/api-contract.md「商品与库存」）：

* ``GET    /tenants/{tenant_id}/products``             商品列表（无成本权限时过滤成本字段）
* ``POST   /tenants/{tenant_id}/products``             新建商品（含 SKU、图片）
* ``GET    /tenants/{tenant_id}/products/{id}``        商品详情（含 SKU、图片）
* ``PATCH  /tenants/{tenant_id}/products/{id}``        局部更新（成本字段需 catalog.cost.read）
* ``POST   /tenants/{tenant_id}/products/{id}/archive`` 归档（状态字段，不物理删除）

路由层只做参数校验、权限依赖与响应包装；业务规则和事务在 ``service``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request

from app.api.deps import (
    TenantCtx,
    TenantDb,
    require_permission,
)
from app.core.context import TenantContext, get_request_id
from app.core.http import PageParams, paged, success
from app.core.permissions import Permission
from app.modules.access.service import AccessSnapshot
from app.modules.catalog import service as catalog_service
from app.modules.catalog.schemas import ProductCreate, ProductUpdate

router = APIRouter(prefix="/tenants/{tenant_id}/products", tags=["catalog"])

# 权限依赖放在 TenantCtx 之后，保证租户上下文与 AccessSnapshot 已装载
ReadAccess = Annotated[
    AccessSnapshot, Depends(require_permission(Permission.CATALOG_PRODUCT_READ))
]
CreateAccess = Annotated[
    AccessSnapshot, Depends(require_permission(Permission.CATALOG_PRODUCT_CREATE))
]
UpdateAccess = Annotated[
    AccessSnapshot, Depends(require_permission(Permission.CATALOG_PRODUCT_UPDATE))
]

ProductId = Annotated[int, Path(gt=0, description="商品ID")]


def _audit_context(request: Request, ctx: TenantContext) -> catalog_service.AuditContext:
    client_ip = request.client.host if request.client is not None else ""
    return catalog_service.AuditContext(
        actor_id=ctx.actor_id or ctx.user_id, request_id=get_request_id(), ip=client_ip
    )


@router.get("", summary="商品列表")
def list_products(
    ctx: TenantCtx,
    db: TenantDb,
    snapshot: ReadAccess,
    page: Annotated[PageParams, Depends()],
    q: str | None = Query(default=None, max_length=100, description="按编码 / 名称 / 品牌搜索"),
    status: str | None = Query(
        default=None, pattern="^(active|archived)$", description="商品状态筛选"
    ),
) -> dict[str, Any]:
    items, total = catalog_service.list_products(
        db,
        tenant_id=ctx.tenant_id,
        effective=snapshot.effective,
        page=page.page,
        page_size=page.page_size,
        q=q,
        status=status,
    )
    return paged(
        [item.model_dump() for item in items], total, page.page, page.page_size
    )


@router.post("", summary="新建商品（含 SKU、图片）")
def create_product(
    ctx: TenantCtx,
    db: TenantDb,
    snapshot: CreateAccess,
    payload: ProductCreate,
    request: Request,
) -> dict[str, Any]:
    detail = catalog_service.create_product(
        db,
        tenant_id=ctx.tenant_id,
        effective=snapshot.effective,
        payload=payload,
        audit=_audit_context(request, ctx),
    )
    return success(detail.model_dump())


@router.get("/{product_id}", summary="商品详情")
def get_product(
    ctx: TenantCtx,
    db: TenantDb,
    snapshot: ReadAccess,
    product_id: ProductId,
) -> dict[str, Any]:
    detail = catalog_service.get_product_detail(
        db,
        tenant_id=ctx.tenant_id,
        product_id=product_id,
        effective=snapshot.effective,
    )
    return success(detail.model_dump())


@router.patch("/{product_id}", summary="局部更新商品")
def update_product(
    ctx: TenantCtx,
    db: TenantDb,
    snapshot: UpdateAccess,
    product_id: ProductId,
    payload: ProductUpdate,
    request: Request,
) -> dict[str, Any]:
    detail = catalog_service.update_product(
        db,
        tenant_id=ctx.tenant_id,
        product_id=product_id,
        effective=snapshot.effective,
        payload=payload,
        audit=_audit_context(request, ctx),
    )
    return success(detail.model_dump())


@router.post("/{product_id}/archive", summary="归档商品")
def archive_product(
    ctx: TenantCtx,
    db: TenantDb,
    snapshot: UpdateAccess,
    product_id: ProductId,
    request: Request,
) -> dict[str, Any]:
    detail = catalog_service.archive_product(
        db,
        tenant_id=ctx.tenant_id,
        product_id=product_id,
        effective=snapshot.effective,
        audit=_audit_context(request, ctx),
    )
    return success(detail.model_dump())


__all__ = ["router"]
