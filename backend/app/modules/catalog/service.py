"""SPU/SKU 服务：共享资料权限、版本校验、别名与字段审计同事务提交。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, load_only

from app.core.access import EffectivePermissions
from app.core.errors import AppError, ConflictError, ErrorCode, PermissionDeniedError
from app.core.permissions import Permission
from app.db.base import utcnow
from app.modules.catalog import repository as catalog_repo
from app.modules.catalog import history
from app.modules.catalog.aggregates import aggregate_product
from app.modules.catalog.identifiers import list_aliases, sync_identifiers
from app.modules.catalog.models_tenant import CatalogOption, Product, ProductImage, ProductSku, Supplier
from app.modules.catalog.schemas import (
    ProductCreate, ProductDetail, ProductDetailPublic, ProductImageOut, ProductListItem,
    ProductListItemPublic, ProductSkuOut, ProductSkuPublicOut, ProductSkuUpdate, ProductUpdate,
)

PRODUCT_COST_FIELDS = frozenset({"cost_amount", "cost_currency"})
SKU_COST_FIELDS = frozenset({"cost_amount", "guidance_cost", "average_cost"})
PRODUCT_FIELDS = (
    "code", "name", "brand", "category", "base_price", "description", "status", "name_en",
    "preferred_supplier_id", "purchaser_id", "developer_id", "inquiry_buyer_id", "hs_code",
    "material_zh", "material_en", "usage", "feature_tags", "packaging_name", "length_cm",
    "width_cm", "height_cm", "package_length_cm", "package_width_cm", "package_height_cm",
    "sales_type", "remark", "features",
)
SKU_FIELDS = ("sku_code", "name", "price", "specs", "status", "image_url", "weight_g", "sales_status")
PRODUCT_NON_NULLABLE = {"code", "name", "base_price", "status", "sales_type"}
SKU_NON_NULLABLE = {"sku_code", "name", "price", "status", "sales_status"}
REFERENCE_IDS = {"preferred_supplier_id", "purchaser_id", "developer_id", "inquiry_buyer_id"}


@dataclass(frozen=True, slots=True)
class AuditContext:
    actor_id: int | None = None
    request_id: str = ""
    ip: str = ""
    actor_name: str = ""
    member_names: dict[str, str] = field(default_factory=dict)


def _require_master_scope(effective: EffectivePermissions, permission: str) -> None:
    if "all" not in effective.granted_scope_types(permission):
        raise PermissionDeniedError("访问共享主商品需要该操作的全部数据范围授权")


def can_read_cost(effective: EffectivePermissions) -> bool:
    return "all" in effective.granted_scope_types(Permission.CATALOG_COST_READ)


def redact_cost(value: Decimal | None, *, can_read: bool) -> Decimal | None:
    return value if can_read else None


def list_products(db: Session, *, tenant_id: int, effective: EffectivePermissions, page: int,
                  page_size: int, q: str | None = None, status: str | None = None,
                  filters: dict | None = None):
    _require_master_scope(effective, "catalog.product.read")
    include_cost = can_read_cost(effective)
    products, total = catalog_repo.list_products(db, tenant_id=tenant_id, page=page,
        page_size=page_size, q=q, status=status, include_cost=include_cost, filters=filters)
    ids = [p.id for p in products]
    grouped = defaultdict(list)
    for sku in catalog_repo.list_skus_for_products(db, tenant_id, ids, include_cost=include_cost):
        grouped[sku.product_id].append(sku)
    images = {}
    if ids:
        for image in db.execute(select(ProductImage).where(ProductImage.tenant_id == tenant_id,
            ProductImage.product_id.in_(ids), ProductImage.sku_id.is_(None))
            .order_by(ProductImage.sort_order, ProductImage.id)).scalars():
            images.setdefault(image.product_id, image.url)
    suppliers = _suppliers(db, tenant_id, [p.preferred_supplier_id for p in products])
    result = []
    for product in products:
        skus = grouped[product.id]
        base = _product_base_kwargs(product)
        base.update(aggregate_product(skus, include_cost=include_cost))
        base["primary_image"] = images.get(product.id)
        base["preferred_supplier_name"] = suppliers.get(product.preferred_supplier_id)
        base["sku_count"] = sum(s.status != "archived" for s in skus)
        if include_cost:
            base.update(cost_amount=_uniform_current(base), cost_currency=product.cost_currency)
        result.append((ProductListItem if include_cost else ProductListItemPublic)(**base))
    return result, total


def get_product_detail(db: Session, *, tenant_id: int, product_id: int,
                       effective: EffectivePermissions):
    _require_master_scope(effective, "catalog.product.read")
    include_cost = can_read_cost(effective)
    product = catalog_repo.get_product(db, tenant_id, product_id, include_cost=include_cost)
    if product is None:
        raise AppError(ErrorCode.PRODUCT_NOT_FOUND)
    return _load_detail(db, tenant_id, product, include_cost=include_cost)


def _supplier_allowed(db, tenant_id, supplier_id):
    if supplier_id is None:
        return
    supplier = db.execute(select(Supplier).where(Supplier.tenant_id == tenant_id,
        Supplier.id == int(supplier_id), Supplier.status == "active")).scalar_one_or_none()
    if supplier is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "首选供应商不存在、已停用或不属于当前租户")


def _validate_option_changes(db, tenant_id, payload, previous=None):
    """停用资料不可新选；历史商品保留原选择，也兼容旧版自由文本。"""
    for field_name, kind, label in (
        ("brand", "brand", "品牌"), ("category", "category", "分类"),
        ("packaging_name", "packaging", "包材"), ("feature_tags", "tag", "特性标签"),
    ):
        if field_name not in payload.model_fields_set:
            continue
        value = getattr(payload, field_name)
        old_value = getattr(previous, field_name, None)
        if field_name == "feature_tags":
            added = set(value or []) - set(old_value or [])
        else:
            added = {value} if value and value != old_value else set()
        if not added:
            continue
        disabled = db.execute(select(CatalogOption.id).where(
            CatalogOption.tenant_id == tenant_id, CatalogOption.kind == kind,
            CatalogOption.name.in_(added), CatalogOption.status != "active",
        ).limit(1)).first()
        if disabled:
            raise AppError(ErrorCode.VALIDATION_ERROR, f"所选{label}已停用，请在商品设置中启用或选择其他资料")


def _ensure_cost_write_allowed(payload, *, include_cost: bool):
    if include_cost:
        return
    if payload.model_fields_set & PRODUCT_COST_FIELDS:
        raise PermissionDeniedError("无成本权限，不能设置成本字段")
    for sku in payload.skus or []:
        if sku.model_fields_set & SKU_COST_FIELDS:
            raise PermissionDeniedError("无成本权限，不能设置SKU成本")


def _commit_or_conflict(db, message):
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(message) from exc


def create_product(db: Session, *, tenant_id: int, effective: EffectivePermissions,
                   payload: ProductCreate, audit: AuditContext):
    _require_master_scope(effective, "catalog.product.create")
    include_cost = can_read_cost(effective)
    _ensure_cost_write_allowed(payload, include_cost=include_cost)
    from app.modules.platform.quotas import ensure_tenant_capacity
    try:
        ensure_tenant_capacity(db, tenant_id, {"max_products": 1, "max_skus": len(payload.skus)})
        _supplier_allowed(db, tenant_id, payload.preferred_supplier_id)
        _validate_option_changes(db, tenant_id, payload)
        if catalog_repo.get_product_by_code(db, tenant_id, payload.code) is not None:
            raise ConflictError("SPU编号已存在")
        values = {key: getattr(payload, key) for key in PRODUCT_FIELDS
                  if hasattr(payload, key) and key != "status"}
        for key in REFERENCE_IDS:
            if values.get(key) is not None:
                values[key] = int(values[key])
        product = Product(tenant_id=tenant_id, status="active", cost_amount=None,
                          cost_currency=payload.cost_currency, **values)
        db.add(product)
        db.flush()
        for item in payload.skus:
            _create_sku(db, tenant_id, product, item, include_cost=include_cost,
                        fallback_cost=payload.cost_amount if include_cost else None)
        _replace_images(db, tenant_id, product, payload.images)
        db.flush()
        after = history.snapshot(db, tenant_id, product, include_cost=include_cost)
        history.record_changes(db, tenant_id, product, {}, after, audit, action="catalog.product.create")
        _commit_or_conflict(db, "SPU编号、SKU编号或别名已存在")
        return _load_detail(db, tenant_id, product, include_cost=include_cost)
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("SPU编号、SKU编号或别名已存在") from exc
    except Exception:
        db.rollback()
        raise


def _lock_product(db, tenant_id, product_id, *, include_cost):
    stmt = select(Product).where(Product.tenant_id == tenant_id, Product.id == product_id)
    if not include_cost:
        stmt = stmt.options(load_only(*(getattr(Product, c.name) for c in Product.__table__.columns
                                       if c.name not in PRODUCT_COST_FIELDS)))
    product = db.execute(stmt.with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if product is None:
        raise AppError(ErrorCode.PRODUCT_NOT_FOUND)
    return product


def update_product(db: Session, *, tenant_id: int, product_id: int, effective: EffectivePermissions,
                   payload: ProductUpdate, audit: AuditContext):
    _require_master_scope(effective, "catalog.product.update")
    include_cost = can_read_cost(effective)
    _ensure_cost_write_allowed(payload, include_cost=include_cost)
    if "cost_amount" in payload.model_fields_set:
        raise AppError(ErrorCode.VALIDATION_ERROR, "SPU成本由SKU汇总，请在销售信息中修改SKU成本")
    from app.modules.platform.quotas import ensure_tenant_capacity
    try:
        additions = sum(not item.id for item in payload.skus or [])
        if additions:
            ensure_tenant_capacity(db, tenant_id, {"max_skus": additions})
        product = _lock_product(db, tenant_id, product_id, include_cost=include_cost)
        if payload.version is not None and payload.version != product.version:
            raise ConflictError("SPU已被其他人修改，请刷新后再保存")
        before = history.snapshot(db, tenant_id, product, include_cost=include_cost)
        fields_set = payload.model_fields_set
        if "preferred_supplier_id" in fields_set and (
            int(payload.preferred_supplier_id) if payload.preferred_supplier_id else None
        ) != product.preferred_supplier_id:
            _supplier_allowed(db, tenant_id, payload.preferred_supplier_id)
        _validate_option_changes(db, tenant_id, payload, product)
        for key in PRODUCT_FIELDS:
            if key not in fields_set:
                continue
            value = getattr(payload, key)
            if value is None and key in PRODUCT_NON_NULLABLE:
                raise AppError(ErrorCode.VALIDATION_ERROR, f"字段{key}不能为空")
            if key == "code" and value != product.code:
                other = catalog_repo.get_product_by_code(db, tenant_id, value)
                if other is not None and other.id != product.id:
                    raise ConflictError("SPU编号已存在")
            if key in REFERENCE_IDS and value is not None:
                value = int(value)
            setattr(product, key, value)
        if include_cost and "cost_currency" in fields_set:
            if not payload.cost_currency:
                raise AppError(ErrorCode.VALIDATION_ERROR, "成本币种不能为空")
            product.cost_currency = payload.cost_currency
        if payload.skus is not None:
            _apply_sku_updates(db, tenant_id=tenant_id, product=product, items=payload.skus,
                               include_cost=include_cost)
        if "images" in fields_set:
            _replace_images(db, tenant_id, product, payload.images or [])
        db.flush()
        skus = catalog_repo.list_skus(db, tenant_id, product.id, include_cost=include_cost)
        if product.status == "active" and not any(s.status != "archived" for s in skus):
            raise AppError(ErrorCode.VALIDATION_ERROR, "正常SPU至少需要一个未归档SKU")
        after = history.snapshot(db, tenant_id, product, include_cost=include_cost)
        if history.differences(before, after):
            product.version += 1
            product.updated_at = utcnow()
            previous = before.get("skus", {})
            for sku in skus:
                key = str(sku.id)
                if key in previous and previous[key] != after["skus"].get(key):
                    sku.version += 1
                    sku.updated_at = utcnow()
            history.record_changes(db, tenant_id, product, before, after, audit)
        _commit_or_conflict(db, "SPU编号、SKU编号或别名已存在")
        return _load_detail(db, tenant_id, product, include_cost=include_cost)
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("SPU编号、SKU编号或别名已存在") from exc
    except Exception:
        db.rollback()
        raise


def _create_sku(db, tenant_id, product, item, *, include_cost, fallback_cost=None):
    if not item.sku_code:
        raise AppError(ErrorCode.VALIDATION_ERROR, "新增SKU必须填写SKU编号")
    if catalog_repo.get_sku_by_code(db, tenant_id, item.sku_code) is not None:
        raise ConflictError("SKU编号已存在")
    sku = ProductSku(tenant_id=tenant_id, product_id=product.id, sku_code=item.sku_code,
        name=item.name or product.name, price=item.price if item.price is not None else Decimal("0"),
        specs=item.specs, status=getattr(item, "status", None) or "active",
        image_url=item.image_url, weight_g=item.weight_g, sales_status=item.sales_status or "on_sale",
        cost_amount=(item.cost_amount if item.cost_amount is not None else fallback_cost) if include_cost else None,
        guidance_cost=item.guidance_cost if include_cost else None,
        average_cost=item.average_cost if include_cost else None)
    db.add(sku)
    db.flush()
    sync_identifiers(db, tenant_id, sku, item.aliases or [])
    return sku


def _apply_sku_updates(db, *, tenant_id, product, items: Sequence[ProductSkuUpdate], include_cost):
    stmt = select(ProductSku).where(ProductSku.tenant_id == tenant_id,
        ProductSku.product_id == product.id).order_by(ProductSku.id).with_for_update()
    if not include_cost:
        stmt = stmt.options(load_only(*(getattr(ProductSku, c.name) for c in ProductSku.__table__.columns
                                       if c.name not in SKU_COST_FIELDS)))
    existing = {sku.id: sku for sku in db.execute(stmt.execution_options(populate_existing=True)).scalars()}
    seen = set()
    for item in items:
        if not item.id:
            _create_sku(db, tenant_id, product, item, include_cost=include_cost)
            continue
        try:
            sku_id = int(item.id)
        except (ValueError, TypeError) as exc:
            raise AppError(ErrorCode.VALIDATION_ERROR, "SKU ID不合法") from exc
        if sku_id in seen:
            raise AppError(ErrorCode.VALIDATION_ERROR, "同一SKU不能在一次请求中重复修改")
        seen.add(sku_id)
        sku = existing.get(sku_id)
        if sku is None:
            raise AppError(ErrorCode.SKU_NOT_FOUND)
        if item.version is not None and item.version != sku.version:
            raise ConflictError("SKU已被其他人修改，请刷新后再保存")
        previous_code = sku.sku_code
        for key in SKU_FIELDS:
            if key not in item.model_fields_set:
                continue
            value = getattr(item, key)
            if value is None and key in SKU_NON_NULLABLE:
                raise AppError(ErrorCode.VALIDATION_ERROR, f"字段{key}不能为空")
            setattr(sku, key, value)
        if include_cost:
            for key in SKU_COST_FIELDS:
                if key in item.model_fields_set:
                    setattr(sku, key, getattr(item, key))
        aliases = item.aliases if "aliases" in item.model_fields_set else None
        sync_identifiers(db, tenant_id, sku, aliases, previous_code=previous_code)


def _replace_images(db, tenant_id, product, urls):
    current = [image.url for image in catalog_repo.list_images(db, tenant_id, product.id)
               if image.sku_id is None]
    if current == list(urls):
        return
    catalog_repo.delete_images(db, tenant_id, product.id)
    for index, url in enumerate(urls):
        db.add(ProductImage(tenant_id=tenant_id, product_id=product.id, sku_id=None,
                            url=url, sort_order=index))


def archive_product(db: Session, *, tenant_id: int, product_id: int,
                    effective: EffectivePermissions, audit: AuditContext):
    return update_product(db, tenant_id=tenant_id, product_id=product_id, effective=effective,
                          payload=ProductUpdate(status="archived"), audit=audit)


def _product_base_kwargs(product):
    result = {key: getattr(product, key) for key in PRODUCT_FIELDS}
    result.update(id=str(product.id), created_at=product.created_at,
                  updated_at=product.updated_at, version=product.version)
    for key in REFERENCE_IDS:
        result[key] = str(result[key]) if result[key] is not None else None
    return result


def _uniform_current(data):
    current = data["cost_summary"]["current"]
    return current["value"] if current["state"] == "uniform" else None


def _suppliers(db, tenant_id, ids):
    ids = {item for item in ids if item is not None}
    if not ids:
        return {}
    return dict(db.execute(select(Supplier.id, Supplier.name).where(
        Supplier.tenant_id == tenant_id, Supplier.id.in_(ids))).all())


def _sku_kwargs(sku, *, include_cost):
    result = {key: getattr(sku, key) for key in SKU_FIELDS}
    result.update(id=str(sku.id), version=sku.version)
    if include_cost:
        result.update({key: getattr(sku, key) for key in SKU_COST_FIELDS})
    return result


def _load_detail(db, tenant_id, product, *, include_cost):
    skus = catalog_repo.list_skus(db, tenant_id, product.id, include_cost=include_cost)
    aliases = list_aliases(db, tenant_id, [s.id for s in skus])
    images = catalog_repo.list_images(db, tenant_id, product.id)
    base = _product_base_kwargs(product)
    base.update(aggregate_product(skus, include_cost=include_cost))
    main_images = [image for image in images if image.sku_id is None]
    base["primary_image"] = main_images[0].url if main_images else None
    base["preferred_supplier_name"] = _suppliers(db, tenant_id, [product.preferred_supplier_id]).get(product.preferred_supplier_id)
    base["images"] = [ProductImageOut(id=str(i.id), url=i.url, sort_order=i.sort_order,
        sku_id=str(i.sku_id) if i.sku_id is not None else None) for i in main_images]
    sku_class = ProductSkuOut if include_cost else ProductSkuPublicOut
    base["skus"] = [sku_class(**_sku_kwargs(s, include_cost=include_cost), aliases=aliases.get(s.id, [])) for s in skus]
    if include_cost:
        base.update(cost_amount=_uniform_current(base), cost_currency=product.cost_currency)
    return (ProductDetail if include_cost else ProductDetailPublic)(**base)


def product_logs(db: Session, *, tenant_id: int, product_id: int, effective: EffectivePermissions,
                 page: int, page_size: int):
    _require_master_scope(effective, "catalog.product.read")
    if catalog_repo.get_product(db, tenant_id, product_id, include_cost=False) is None:
        raise AppError(ErrorCode.PRODUCT_NOT_FOUND)
    from app.modules.audit.models_tenant import AuditLog
    from app.modules.audit.service import visible_payload
    conditions = (AuditLog.tenant_id == tenant_id, or_(AuditLog.product_id == product_id,
        (AuditLog.resource_type == "product") & (AuditLog.resource_id == product_id)))
    total = int(db.execute(select(func.count()).select_from(AuditLog).where(*conditions)).scalar_one())
    rows = db.execute(select(AuditLog).where(*conditions).order_by(AuditLog.id.desc())
        .offset(max(page - 1, 0) * page_size).limit(page_size)).scalars()
    items = []
    for row in rows:
        payload = visible_payload(row.payload, include_cost=can_read_cost(effective)) or {}
        items.append({"id": str(row.id), "summary": row.summary,
            "actor_name": row.actor_name_snapshot or ("系统" if not row.actor_id else str(row.actor_id)),
            "created_at": row.created_at.isoformat(timespec="seconds") + "Z",
            "changes": payload.get("changes", [])})
    return items, total


__all__ = ["AuditContext", "can_read_cost", "redact_cost", "list_products", "get_product_detail",
           "create_product", "update_product", "archive_product", "product_logs"]
