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

from fastapi import APIRouter, Depends, Path, Query, Request, UploadFile, File
from fastapi.responses import FileResponse, RedirectResponse

from app.api.deps import (
    TenantCtx,
    TenantDb,
    require_permission,
    CenterDb, AccessSnapshotDep,
)
from app.core.context import TenantContext, get_request_id
from app.core.http import PageParams, paged, success
from app.core.permissions import Permission
from app.modules.access.service import AccessSnapshot
from app.modules.catalog import service as catalog_service
from app.modules.catalog.schemas import ProductCreate, ProductUpdate, ProductSkuCreate, ProductSkuUpdate
from app.modules.catalog import resources

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


def _audit_context(request: Request, ctx: TenantContext, member_names=None) -> catalog_service.AuditContext:
    client_ip = request.client.host if request.client is not None else ""
    return catalog_service.AuditContext(
        actor_id=ctx.actor_id or ctx.user_id, request_id=get_request_id(), ip=client_ip,
        actor_name=getattr(getattr(request.state,"current_user",None),"display_name","") or str(ctx.user_id),
        member_names=member_names or {},
    )


@router.get("", summary="商品列表")
def list_products(
    ctx: TenantCtx,
    db: TenantDb,
    snapshot: ReadAccess,
    page: Annotated[PageParams, Depends()],
    q: str | None = Query(default=None, max_length=200, description="按SPU/SKU/别名/中英文名称搜索"),
    status: str | None = Query(
        default=None, pattern="^(active|archived)$", description="商品状态筛选"
    ),
    sales_status: str | None = Query(default=None, pattern="^(on_sale|discontinued|trial|clearance|partial_clearance|partial_discontinued)$"),
    sales_type: str | None = Query(default=None, pattern="^(normal|variation|bundle|assembly)$"),
    brand: str | None = Query(default=None,max_length=100),
    category: str | None = Query(default=None,max_length=100),
    preferred_supplier_id: int | None = Query(default=None,gt=0),
    purchaser_id: int | None = Query(default=None,gt=0),
    developer_id: int | None = Query(default=None,gt=0),
    inquiry_buyer_id: int | None = Query(default=None,gt=0),
    tag: str | None = Query(default=None,max_length=100),
) -> dict[str, Any]:
    items, total = catalog_service.list_products(
        db,
        tenant_id=ctx.tenant_id,
        effective=snapshot.effective,
        page=page.page,
        page_size=page.page_size,
        q=q,
        status=status,
        filters=dict(sales_status=sales_status,sales_type=sales_type,brand=brand,category=category,
            preferred_supplier_id=preferred_supplier_id,purchaser_id=purchaser_id,developer_id=developer_id,
            inquiry_buyer_id=inquiry_buyer_id,tag=tag),
    )
    return paged(
        [item.model_dump(mode="json") for item in items], total, page.page, page.page_size
    )


@router.post("", summary="新建商品（含 SKU、图片）")
def create_product(
    ctx: TenantCtx,
    db: TenantDb,
    snapshot: CreateAccess,
    payload: ProductCreate,
    request: Request,
    center: CenterDb,
) -> dict[str, Any]:
    catalog_service._require_master_scope(snapshot.effective,"catalog.product.create")
    resources.validate_media_refs(db,ctx.tenant_id,payload)
    detail = catalog_service.create_product(
        db,
        tenant_id=ctx.tenant_id,
        effective=snapshot.effective,
        payload=payload,
        audit=_audit_context(request, ctx,resources.validate_members(center,ctx.tenant_id,payload)),
    )
    return success(detail.model_dump(mode="json"))


@router.get("/options", summary="商品基础资料选项")
def product_options(ctx: TenantCtx, db: TenantDb, center: CenterDb, snapshot: ReadAccess):
    catalog_service._require_master_scope(snapshot.effective,"catalog.product.read")
    data=resources.get_options(db,center,ctx.tenant_id)
    data["can_read_cost"]=catalog_service.can_read_cost(snapshot.effective)
    data["can_manage_options"]="all" in snapshot.effective.granted_scope_types(Permission.CATALOG_PRODUCT_UPDATE)
    return success(data)


@router.post("/options", summary="新增品牌分类标签包材")
def create_option(body: resources.OptionIn, ctx: TenantCtx, db: TenantDb, snapshot: UpdateAccess):
    catalog_service._require_master_scope(snapshot.effective,"catalog.product.update")
    return success(resources.create_resource(db,ctx.tenant_id,body,ctx.user_id))


@router.post("/suppliers", summary="新增供应商简档")
def create_supplier(body: resources.SupplierIn, ctx: TenantCtx, db: TenantDb, snapshot: UpdateAccess):
    catalog_service._require_master_scope(snapshot.effective,"catalog.product.update")
    return success(resources.create_resource(db,ctx.tenant_id,body,ctx.user_id))


@router.patch("/options/{option_id}", summary="编辑品牌分类标签包材")
def update_option(body: resources.OptionUpdate, ctx: TenantCtx, db: TenantDb, snapshot: UpdateAccess, option_id: int=Path(gt=0)):
    catalog_service._require_master_scope(snapshot.effective,"catalog.product.update")
    return success(resources.update_resource(db,ctx.tenant_id,option_id,body,ctx.user_id))


@router.patch("/suppliers/{supplier_id}", summary="编辑供应商简档")
def update_supplier(body: resources.SupplierUpdate, ctx: TenantCtx, db: TenantDb, snapshot: UpdateAccess, supplier_id: int=Path(gt=0)):
    catalog_service._require_master_scope(snapshot.effective,"catalog.product.update")
    return success(resources.update_resource(db,ctx.tenant_id,supplier_id,body,ctx.user_id))


@router.post("/media", summary="上传商品图片")
def upload_media(ctx: TenantCtx, db: TenantDb, center: CenterDb, snapshot: AccessSnapshotDep, file: UploadFile=File(...)):
    from app.core.errors import PermissionDeniedError, AppError, ErrorCode
    if not any("all" in snapshot.effective.granted_scope_types(p) for p in ("catalog.product.create","catalog.product.update")):
        raise PermissionDeniedError("上传商品图片需要商品创建或编辑权限")
    data=file.file.read(10*1024*1024+1)
    if len(data)>10*1024*1024:
        raise AppError(ErrorCode.VALIDATION_ERROR,"图片不能超过10MB")
    return success(resources.save_image(db,ctx.tenant_id,ctx.user_id,file.filename,data,center=center))


@router.get("/media/{file_id}", summary="读取商品图片")
def read_media(ctx: TenantCtx, db: TenantDb, snapshot: ReadAccess, file_id: int=Path(gt=0)):
    catalog_service._require_master_scope(snapshot.effective,"catalog.product.read")
    row=resources.get_media_record(db,ctx.tenant_id,file_id)
    if row.storage_provider!="local":
        if not row.url or not row.url.startswith("https://"):
            from app.core.errors import AppError,ErrorCode
            raise AppError(ErrorCode.CONFLICT,"云文件缺少有效访问地址")
        return RedirectResponse(row.url,status_code=307,headers={"Cache-Control":"private, no-store"})
    path,mime=resources.image_file(db,ctx.tenant_id,file_id)
    return FileResponse(path,media_type=mime,headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})


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
    return success(detail.model_dump(mode="json"))


@router.patch("/{product_id}", summary="局部更新商品")
def update_product(
    ctx: TenantCtx,
    db: TenantDb,
    snapshot: UpdateAccess,
    product_id: ProductId,
    payload: ProductUpdate,
    request: Request,
    center: CenterDb,
) -> dict[str, Any]:
    catalog_service._require_master_scope(snapshot.effective,"catalog.product.update")
    resources.validate_media_refs(db,ctx.tenant_id,payload)
    detail = catalog_service.update_product(
        db,
        tenant_id=ctx.tenant_id,
        product_id=product_id,
        effective=snapshot.effective,
        payload=payload,
        audit=_audit_context(request, ctx,resources.validate_members(center,ctx.tenant_id,payload)),
    )
    return success(detail.model_dump(mode="json"))


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
    return success(detail.model_dump(mode="json"))


@router.post("/{product_id}/skus", summary="新增SKU")
def create_sku(ctx: TenantCtx, db: TenantDb, snapshot: UpdateAccess, product_id: ProductId, payload: ProductSkuCreate, request: Request):
    catalog_service._require_master_scope(snapshot.effective,"catalog.product.update")
    item=ProductSkuUpdate.model_validate(payload.model_dump(exclude_unset=True))
    resources.validate_media_refs(db,ctx.tenant_id,ProductUpdate(skus=[item]))
    detail=catalog_service.update_product(db,tenant_id=ctx.tenant_id,product_id=product_id,effective=snapshot.effective,
        payload=ProductUpdate(skus=[item]),audit=_audit_context(request,ctx))
    return success(detail.model_dump(mode="json"))


@router.patch("/{product_id}/skus/{sku_id}", summary="修改SKU及别名")
def update_sku(ctx: TenantCtx, db: TenantDb, snapshot: UpdateAccess, product_id: ProductId, payload: ProductSkuUpdate, request: Request, sku_id: int=Path(gt=0)):
    catalog_service._require_master_scope(snapshot.effective,"catalog.product.update")
    item=payload.model_copy(update={"id":str(sku_id)})
    resources.validate_media_refs(db,ctx.tenant_id,ProductUpdate(skus=[item]))
    detail=catalog_service.update_product(db,tenant_id=ctx.tenant_id,product_id=product_id,effective=snapshot.effective,
        payload=ProductUpdate(skus=[item]),audit=_audit_context(request,ctx))
    return success(detail.model_dump(mode="json"))


@router.get("/{product_id}/logs", summary="SPU及SKU操作日志")
def product_logs(ctx: TenantCtx, db: TenantDb, snapshot: ReadAccess, product_id: ProductId, page: Annotated[PageParams,Depends()]):
    items,total=catalog_service.product_logs(db,tenant_id=ctx.tenant_id,product_id=product_id,effective=snapshot.effective,page=page.page,page_size=page.page_size)
    return paged(items,total,page.page,page.page_size)


__all__ = ["router"]
