"""商品业务服务：业务规则、事务边界、成本字段权限过滤与审计写入。

事务约定（见 docs/architecture.md「事务由 Service 管理」）：

* 写操作的 ``session.commit()`` 在本层完成，Router 不提交事务；
* 业务写入与审计写入处于同一租户库事务，审计失败则整体不提交；
* ``audit.service.record`` 由 backend-identity 实现，函数内延迟导入，避免并行开发期的循环依赖。

成本字段约定（见 plan.txt §3 / 验证项 3）：

* 无 ``catalog.cost.read`` 权限时，列表 / 详情查询不读取成本列，响应使用不带成本的
  ``*PublicOut`` 模型；
* 无权限时请求体中的成本字段一律拒绝（403），不允许写入。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.access import EffectivePermissions
from app.core.errors import (
    AppError,
    ConflictError,
    ErrorCode,
    PermissionDeniedError,
)
from app.core.permissions import Permission
from app.modules.catalog import repository as catalog_repo
from app.modules.catalog.models_tenant import Product, ProductImage, ProductSku
from app.modules.catalog.schemas import (
    ProductCreate,
    ProductDetail,
    ProductDetailPublic,
    ProductImageOut,
    ProductListItem,
    ProductListItemPublic,
    ProductSkuOut,
    ProductSkuPublicOut,
    ProductSkuUpdate,
    ProductUpdate,
)

# 成本相关字段：无 catalog.cost.read 权限时不可读也不可写
PRODUCT_COST_FIELDS: frozenset[str] = frozenset({"cost_amount", "cost_currency"})
SKU_COST_FIELDS: frozenset[str] = frozenset({"cost_amount"})

# 局部更新时不允许置空的必填字段
_PRODUCT_NON_NULLABLE = frozenset({"code", "name", "base_price", "status", "cost_currency"})
_SKU_NON_NULLABLE = frozenset({"sku_code", "name", "price", "status"})

_PRODUCT_UPDATABLE_FIELDS = (
    "code",
    "name",
    "brand",
    "category",
    "base_price",
    "description",
    "status",
)


@dataclass(frozen=True, slots=True)
class AuditContext:
    """审计上下文：由 Router 从请求中提取，避免 Service 依赖 FastAPI Request。"""

    actor_id: int | None = None
    request_id: str = ""
    ip: str = ""


# --------------------------------------------------------------------------- 成本过滤（纯函数）
def _require_master_scope(effective: EffectivePermissions, permission: str) -> None:
    # 主商品属于全租户共享资料；店铺权限只能修改店铺刊登，不得影响其他店铺。
    if "all" not in effective.granted_scope_types(permission):
        raise PermissionDeniedError("访问共享主商品需要该操作的全部数据范围授权")


def can_read_cost(effective: EffectivePermissions) -> bool:
    """当前授权是否包含 ``catalog.cost.read``。"""
    return "all" in effective.granted_scope_types(Permission.CATALOG_COST_READ)


def redact_cost(value: Decimal | None, *, can_read: bool) -> Decimal | None:
    """无读取权限时返回 ``None``，保证成本值不进入响应数据。"""
    return value if can_read else None


# --------------------------------------------------------------------------- 查询
def list_products(
    db: Session,
    *,
    tenant_id: int,
    effective: EffectivePermissions,
    page: int,
    page_size: int,
    q: str | None = None,
    status: str | None = None,
) -> tuple[list[ProductListItem | ProductListItemPublic], int]:
    """商品分页列表；无成本权限时使用不含成本字段的响应模型。"""
    _require_master_scope(effective, "catalog.product.read")
    include_cost = can_read_cost(effective)
    products, total = catalog_repo.list_products(
        db,
        tenant_id=tenant_id,
        page=page,
        page_size=page_size,
        q=q,
        status=status,
        include_cost=include_cost,
    )
    sku_counts = catalog_repo.count_skus_by_products(
        db, tenant_id, [product.id for product in products]
    )
    items: list[ProductListItem | ProductListItemPublic] = [
        _list_item(product, sku_counts.get(product.id, 0), include_cost=include_cost)
        for product in products
    ]
    return items, total


def get_product_detail(
    db: Session,
    *,
    tenant_id: int,
    product_id: int,
    effective: EffectivePermissions,
) -> ProductDetail | ProductDetailPublic:
    _require_master_scope(effective, "catalog.product.read")
    include_cost = can_read_cost(effective)
    product = catalog_repo.get_product(
        db, tenant_id, product_id, include_cost=include_cost
    )
    if product is None:
        raise AppError(ErrorCode.PRODUCT_NOT_FOUND)
    return _load_detail(db, tenant_id, product, include_cost=include_cost)


# --------------------------------------------------------------------------- 写入
def create_product(
    db: Session,
    *,
    tenant_id: int,
    effective: EffectivePermissions,
    payload: ProductCreate,
    audit: AuditContext,
) -> ProductDetail | ProductDetailPublic:
    """新建商品 + SKU + 图片（同一事务）；编码重复返回 409。"""
    _require_master_scope(effective, "catalog.product.create")
    include_cost = can_read_cost(effective)
    _ensure_cost_write_allowed(payload, include_cost=include_cost)

    from app.modules.platform.quotas import ensure_tenant_capacity
    ensure_tenant_capacity(db, tenant_id, {'max_products': 1, 'max_skus': len(payload.skus)})

    if catalog_repo.get_product_by_code(db, tenant_id, payload.code) is not None:
        raise ConflictError("商品编码已存在", details={"field": "code"})
    for item in payload.skus:
        if catalog_repo.get_sku_by_code(db, tenant_id, item.sku_code) is not None:
            raise ConflictError(
                "SKU 编码已存在",
                details={"field": "sku_code", "sku_code": item.sku_code},
            )

    product = Product(
        tenant_id=tenant_id,
        code=payload.code,
        name=payload.name,
        brand=payload.brand,
        category=payload.category,
        status="active",
        cost_amount=payload.cost_amount if include_cost else None,
        cost_currency=payload.cost_currency,
        base_price=payload.base_price,
        description=payload.description,
    )
    db.add(product)
    try:
        db.flush()
        for item in payload.skus:
            db.add(
                ProductSku(
                    tenant_id=tenant_id,
                    product_id=product.id,
                    sku_code=item.sku_code,
                    name=item.name,
                    specs=item.specs,
                    price=item.price,
                    cost_amount=item.cost_amount if include_cost else None,
                    status="active",
                )
            )
        for index, url in enumerate(payload.images):
            db.add(
                ProductImage(
                    tenant_id=tenant_id,
                    product_id=product.id,
                    sku_id=None,
                    url=url,
                    sort_order=index,
                )
            )
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("商品编码或 SKU 编码已存在") from exc

    _write_audit(
        db,
        tenant_id=tenant_id,
        action="catalog.product.create",
        resource_type="product",
        resource_id=product.id,
        audit=audit,
        summary=f"创建商品 {product.code}",
        payload={
            "code": product.code,
            "name": product.name,
            "sku_count": len(payload.skus),
            "image_count": len(payload.images),
        },
    )
    _commit_or_conflict(db, "商品编码或 SKU 编码已存在")
    return _load_detail(db, tenant_id, product, include_cost=include_cost)


def update_product(
    db: Session,
    *,
    tenant_id: int,
    product_id: int,
    effective: EffectivePermissions,
    payload: ProductUpdate,
    audit: AuditContext,
) -> ProductDetail | ProductDetailPublic:
    """局部更新商品 / SKU / 图片；成本字段需要 ``catalog.cost.read``。"""
    _require_master_scope(effective, "catalog.product.update")
    include_cost = can_read_cost(effective)
    _ensure_cost_write_allowed(payload, include_cost=include_cost)

    product = catalog_repo.get_product(db, tenant_id, product_id, include_cost=include_cost)
    if product is None:
        raise AppError(ErrorCode.PRODUCT_NOT_FOUND)

    fields_set = payload.model_fields_set
    if "skus" in fields_set and payload.skus is not None:
        from app.modules.platform.quotas import ensure_tenant_capacity
        ensure_tenant_capacity(db, tenant_id, {
            'max_skus': sum(1 for item in payload.skus if not item.id),
        })
    _apply_product_fields(db, tenant_id, product, payload, fields_set, include_cost=include_cost)

    if "skus" in fields_set and payload.skus is not None:
        _apply_sku_updates(db, tenant_id=tenant_id, product=product, items=payload.skus, include_cost=include_cost)

    if "images" in fields_set:
        catalog_repo.delete_images(db, tenant_id, product.id)
        for index, url in enumerate(payload.images or []):
            db.add(
                ProductImage(
                    tenant_id=tenant_id,
                    product_id=product.id,
                    sku_id=None,
                    url=url,
                    sort_order=index,
                )
            )

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("商品编码或 SKU 编码已存在") from exc

    _write_audit(
        db,
        tenant_id=tenant_id,
        action="catalog.product.update",
        resource_type="product",
        resource_id=product.id,
        audit=audit,
        summary=f"更新商品 {product.code}",
        payload={"fields": sorted(fields_set)},
    )
    _commit_or_conflict(db, "商品编码或 SKU 编码已存在")
    return _load_detail(db, tenant_id, product, include_cost=include_cost)


def archive_product(
    db: Session,
    *,
    tenant_id: int,
    product_id: int,
    effective: EffectivePermissions,
    audit: AuditContext,
) -> ProductDetail | ProductDetailPublic:
    """归档商品（状态字段，不物理删除）；重复归档幂等。"""
    _require_master_scope(effective, "catalog.product.update")
    include_cost = can_read_cost(effective)
    product = catalog_repo.get_product(db, tenant_id, product_id, include_cost=include_cost)
    if product is None:
        raise AppError(ErrorCode.PRODUCT_NOT_FOUND)

    if product.status != "archived":
        product.status = "archived"
        _write_audit(
            db,
            tenant_id=tenant_id,
            action="catalog.product.archive",
            resource_type="product",
            resource_id=product.id,
            audit=audit,
            summary=f"归档商品 {product.code}",
            payload={"code": product.code},
        )
        _commit_or_conflict(db, "商品归档失败，请重试")
    return _load_detail(db, tenant_id, product, include_cost=include_cost)


# --------------------------------------------------------------------------- 内部实现
def _list_item(
    product: Product, sku_count: int, *, include_cost: bool
) -> ProductListItem | ProductListItemPublic:
    base = _product_base_kwargs(product)
    if include_cost:
        return ProductListItem(
            **base,
            sku_count=sku_count,
            cost_amount=product.cost_amount,
            cost_currency=product.cost_currency,
        )
    return ProductListItemPublic(**base, sku_count=sku_count)


def _load_detail(
    db: Session, tenant_id: int, product: Product, *, include_cost: bool
) -> ProductDetail | ProductDetailPublic:
    skus = catalog_repo.list_skus(db, tenant_id, product.id, include_cost=include_cost)
    images = catalog_repo.list_images(db, tenant_id, product.id)
    image_items = [
        ProductImageOut(
            id=str(image.id),
            url=image.url,
            sort_order=image.sort_order,
            sku_id=str(image.sku_id) if image.sku_id is not None else None,
        )
        for image in images
    ]
    base = _product_base_kwargs(product)
    if include_cost:
        return ProductDetail(
            **base,
            cost_amount=product.cost_amount,
            cost_currency=product.cost_currency,
            skus=[ProductSkuOut(**_sku_kwargs(sku, include_cost=True)) for sku in skus],
            images=image_items,
        )
    return ProductDetailPublic(
        **base,
        skus=[ProductSkuPublicOut(**_sku_kwargs(sku, include_cost=False)) for sku in skus],
        images=image_items,
    )


def _product_base_kwargs(product: Product) -> dict[str, Any]:
    return {
        "id": str(product.id),
        "code": product.code,
        "name": product.name,
        "brand": product.brand,
        "category": product.category,
        "status": product.status,
        "base_price": product.base_price,
        "description": product.description,
        "created_at": product.created_at,
        "updated_at": product.updated_at,
    }


def _sku_kwargs(sku: ProductSku, *, include_cost: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(sku.id),
        "sku_code": sku.sku_code,
        "name": sku.name,
        "specs": sku.specs,
        "price": sku.price,
        "status": sku.status,
    }
    if include_cost:
        data["cost_amount"] = sku.cost_amount
    return data


def _apply_product_fields(
    db: Session,
    tenant_id: int,
    product: Product,
    payload: ProductUpdate,
    fields_set: set[str],
    *,
    include_cost: bool,
) -> None:
    for field in _PRODUCT_UPDATABLE_FIELDS:
        if field not in fields_set:
            continue
        value = getattr(payload, field)
        if value is None and field in _PRODUCT_NON_NULLABLE:
            raise AppError(ErrorCode.VALIDATION_ERROR, f"字段 {field} 不能为空")
        if field == "code" and value != product.code:
            existing = catalog_repo.get_product_by_code(db, tenant_id, value)
            if existing is not None and existing.id != product.id:
                raise ConflictError("商品编码已存在", details={"field": "code"})
        setattr(product, field, value)

    if include_cost:
        if "cost_amount" in fields_set:
            product.cost_amount = payload.cost_amount
        if "cost_currency" in fields_set:
            if payload.cost_currency is None:
                raise AppError(ErrorCode.VALIDATION_ERROR, "字段 cost_currency 不能为空")
            product.cost_currency = payload.cost_currency


def _apply_sku_updates(
    db: Session,
    *,
    tenant_id: int,
    product: Product,
    items: Sequence[ProductSkuUpdate],
    include_cost: bool,
) -> None:
    existing = {
        sku.id: sku
        for sku in catalog_repo.list_skus(
            db, tenant_id, product.id, include_cost=include_cost
        )
    }
    for item in items:
        if item.id:
            try:
                sku_id = int(item.id)
            except (TypeError, ValueError) as exc:
                raise AppError(ErrorCode.VALIDATION_ERROR, "SKU ID 不合法") from exc
            sku = existing.get(sku_id)
            if sku is None:
                raise AppError(ErrorCode.SKU_NOT_FOUND)
            _apply_sku_fields(db, tenant_id, sku, item, include_cost=include_cost)
        else:
            _create_sku_from_update(db, tenant_id, product, item, include_cost=include_cost)


def _apply_sku_fields(
    db: Session,
    tenant_id: int,
    sku: ProductSku,
    item: ProductSkuUpdate,
    *,
    include_cost: bool,
) -> None:
    fields_set = item.model_fields_set
    for field in ("sku_code", "name", "price", "specs", "status"):
        if field not in fields_set:
            continue
        value = getattr(item, field)
        if value is None and field in _SKU_NON_NULLABLE:
            raise AppError(ErrorCode.VALIDATION_ERROR, f"字段 {field} 不能为空")
        if field == "sku_code" and value != sku.sku_code:
            other = catalog_repo.get_sku_by_code(db, tenant_id, value)
            if other is not None and other.id != sku.id:
                raise ConflictError(
                    "SKU 编码已存在",
                    details={"field": "sku_code", "sku_code": value},
                )
        setattr(sku, field, value)

    if include_cost and "cost_amount" in fields_set:
        sku.cost_amount = item.cost_amount


def _create_sku_from_update(
    db: Session,
    tenant_id: int,
    product: Product,
    item: ProductSkuUpdate,
    *,
    include_cost: bool,
) -> None:
    missing = [
        name
        for name, value in (
            ("sku_code", item.sku_code),
            ("name", item.name),
            ("price", item.price),
        )
        if value is None
    ]
    if missing:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "新增 SKU 缺少必填字段: " + ", ".join(missing),
        )
    assert item.sku_code is not None and item.name is not None and item.price is not None
    if catalog_repo.get_sku_by_code(db, tenant_id, item.sku_code) is not None:
        raise ConflictError(
            "SKU 编码已存在",
            details={"field": "sku_code", "sku_code": item.sku_code},
        )
    db.add(
        ProductSku(
            tenant_id=tenant_id,
            product_id=product.id,
            sku_code=item.sku_code,
            name=item.name,
            specs=item.specs,
            price=item.price,
            cost_amount=item.cost_amount if include_cost else None,
            status=item.status or "active",
        )
    )


def _ensure_cost_write_allowed(
    payload: ProductCreate | ProductUpdate, *, include_cost: bool
) -> None:
    """无成本读取权限时，拒绝任何成本字段写入（避免越权读写成本）。"""
    if include_cost:
        return
    fields_set = payload.model_fields_set
    if fields_set & PRODUCT_COST_FIELDS:
        raise PermissionDeniedError("无成本读取权限，不能设置成本字段")
    for item in getattr(payload, "skus", None) or []:
        if item.model_fields_set & SKU_COST_FIELDS:
            raise PermissionDeniedError("无成本读取权限，不能设置 SKU 成本")


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
    """写租户审计（与业务同事务）。延迟导入避免与 backend-identity 并行开发时循环依赖。"""
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


def _commit_or_conflict(db: Session, message: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(message) from exc


__all__ = [
    "AuditContext",
    "can_read_cost",
    "redact_cost",
    "list_products",
    "get_product_detail",
    "create_product",
    "update_product",
    "archive_product",
]
