"""商品数据访问层：只做 SQLAlchemy 查询，不写业务规则、不提交事务。

* 所有查询强制带 ``tenant_id`` 条件（租户库由数据库隔离，但代码层面仍需显式约束）。
* 无 ``catalog.cost.read`` 权限时使用 ``load_only`` 不读取成本列，从数据访问层就避免成本进入内存。
* 禁止外键，SKU 与商品通过普通 ID 列 + 显式 join 关联。
"""

from __future__ import annotations

from collections.abc import Sequence
import json

from sqlalchemy import Select, func, or_, select, case
from sqlalchemy.orm import Session, load_only

from app.modules.catalog.models_tenant import Product, ProductImage, ProductSku, SkuIdentifier

# 无成本权限时只加载下列列；成本列既不查询也不进入 ORM 对象
_PRODUCT_BASE_COLUMNS = (
    Product.id,
    Product.tenant_id,
    Product.code,
    Product.name,
    Product.brand,
    Product.category,
    Product.status,
    Product.base_price,
    Product.description,
    Product.created_at,
    Product.updated_at,
)

# 无成本权限时不查询 SKU 成本列
_SKU_BASE_COLUMNS = (
    ProductSku.id,
    ProductSku.tenant_id,
    ProductSku.product_id,
    ProductSku.sku_code,
    ProductSku.name,
    ProductSku.specs,
    ProductSku.price,
    ProductSku.status,
    ProductSku.created_at,
    ProductSku.updated_at,
)

# 关键字搜索命中的商品/SKU 列表上限，避免模糊搜索拉全表
_SEARCH_LIMIT = 500


def _product_stmt(include_cost: bool) -> Select[tuple[Product]]:
    stmt = select(Product)
    if not include_cost:
        stmt = stmt.options(load_only(*(getattr(Product, c.name) for c in Product.__table__.columns
                                      if c.name not in {"cost_amount", "cost_currency"})))
    return stmt


def list_products(
    db: Session,
    *,
    tenant_id: int,
    page: int,
    page_size: int,
    q: str | None = None,
    status: str | None = None,
    include_cost: bool = True,
    filters: dict | None = None,
) -> tuple[list[Product], int]:
    """分页查询商品列表，返回 ``(商品列表, 总条数)``。"""
    conditions = [Product.tenant_id == tenant_id]
    if status:
        conditions.append(Product.status == status)
    keyword = (q or "").strip()
    if keyword:
        like = f"%{keyword}%"
        matching_skus = select(ProductSku.product_id).where(
            ProductSku.tenant_id == tenant_id,
            or_(ProductSku.sku_code.like(like), ProductSku.id.in_(
                select(SkuIdentifier.sku_id).where(SkuIdentifier.tenant_id == tenant_id,
                    SkuIdentifier.is_active.is_(True), SkuIdentifier.identifier.like(like))))
        )
        conditions.append(
            or_(Product.code.like(like), Product.name.like(like), Product.name_en.like(like),
                Product.brand.like(like), Product.id.in_(matching_skus))
        )
    for key, value in (filters or {}).items():
        if value is None or value == "":
            continue
        if key == "sales_status":
            conditions.append(sales_status_expression(tenant_id) == value)
        elif key == "tag":
            if db.get_bind().dialect.name == "sqlite":
                tags = func.json_each(Product.feature_tags).table_valued("value")
                conditions.append(select(tags.c.value).where(tags.c.value == value).exists())
            else:
                conditions.append(func.json_contains(Product.feature_tags, json.dumps(value, ensure_ascii=False)) == 1)
        elif key in {"sales_type", "brand", "category", "preferred_supplier_id", "purchaser_id", "developer_id", "inquiry_buyer_id"}:
            conditions.append(getattr(Product, key) == value)
    total = int(
        db.execute(select(func.count()).select_from(Product).where(*conditions)).scalar_one()
    )
    stmt = (
        _product_stmt(include_cost)
        .where(*conditions)
        .order_by(Product.id.desc())
        .offset(max(page - 1, 0) * page_size)
        .limit(page_size)
    )
    return list(db.execute(stmt).scalars().all()), total


def get_product(
    db: Session, tenant_id: int, product_id: int, *, include_cost: bool = True
) -> Product | None:
    stmt = _product_stmt(include_cost).where(
        Product.tenant_id == tenant_id, Product.id == product_id
    )
    return db.execute(stmt).scalar_one_or_none()


def get_product_by_code(db: Session, tenant_id: int, code: str) -> Product | None:
    stmt = select(Product).where(Product.tenant_id == tenant_id, Product.code == code)
    return db.execute(stmt).scalar_one_or_none()


def list_skus(
    db: Session, tenant_id: int, product_id: int, *, include_cost: bool = True
) -> list[ProductSku]:
    stmt = select(ProductSku).where(
        ProductSku.tenant_id == tenant_id, ProductSku.product_id == product_id
    )
    if not include_cost:
        stmt = stmt.options(load_only(*(getattr(ProductSku, c.name) for c in ProductSku.__table__.columns
                                      if c.name not in {"cost_amount", "guidance_cost", "average_cost"})))
    stmt = stmt.order_by(ProductSku.id)
    return list(db.execute(stmt).scalars().all())


def count_skus_by_products(
    db: Session, tenant_id: int, product_ids: Sequence[int]
) -> dict[int, int]:
    if not product_ids:
        return {}
    rows = db.execute(
        select(ProductSku.product_id, func.count())
        .where(ProductSku.tenant_id == tenant_id, ProductSku.product_id.in_(list(product_ids)))
        .group_by(ProductSku.product_id)
    ).all()
    return {int(product_id): int(count) for product_id, count in rows}


def get_sku(db: Session, tenant_id: int, sku_id: int) -> ProductSku | None:
    stmt = select(ProductSku).where(
        ProductSku.tenant_id == tenant_id, ProductSku.id == sku_id
    )
    return db.execute(stmt).scalar_one_or_none()


def get_sku_by_code(db: Session, tenant_id: int, sku_code: str) -> ProductSku | None:
    stmt = select(ProductSku).where(
        ProductSku.tenant_id == tenant_id, ProductSku.sku_code == sku_code
    )
    return db.execute(stmt).scalar_one_or_none()


def list_images(db: Session, tenant_id: int, product_id: int) -> list[ProductImage]:
    stmt = (
        select(ProductImage)
        .where(ProductImage.tenant_id == tenant_id, ProductImage.product_id == product_id)
        .order_by(ProductImage.sort_order, ProductImage.id)
    )
    return list(db.execute(stmt).scalars().all())


def delete_images(db: Session, tenant_id: int, product_id: int) -> None:
    """删除商品全部图片（图片为附属资料，编辑时整体替换）。"""
    for image in list_images(db, tenant_id, product_id):
        if image.sku_id is None:
            db.delete(image)


def sales_status_expression(tenant_id: int):
    def has(value):
        return select(ProductSku.id).where(ProductSku.tenant_id == tenant_id,
            ProductSku.product_id == Product.id, ProductSku.status != "archived",
            ProductSku.sales_status == value).exists()
    on, stopped, trial, clearance = (has(v) for v in ("on_sale", "discontinued", "trial", "clearance"))
    return case(
        (clearance & ~on & ~stopped & ~trial, "clearance"),
        (stopped & ~on & ~trial & ~clearance, "discontinued"),
        (trial & ~on & ~stopped & ~clearance, "trial"),
        (clearance, "partial_clearance"), (stopped, "partial_discontinued"),
        (on, "on_sale"), else_="discontinued")


def list_skus_for_products(db: Session, tenant_id: int, product_ids: Sequence[int], *, include_cost=True):
    if not product_ids:
        return []
    stmt = select(ProductSku).where(ProductSku.tenant_id == tenant_id, ProductSku.product_id.in_(product_ids)).order_by(ProductSku.id)
    if not include_cost:
        stmt = stmt.options(load_only(*(getattr(ProductSku, c.name) for c in ProductSku.__table__.columns
                                      if c.name not in {"cost_amount", "guidance_cost", "average_cost"})))
    return list(db.execute(stmt).scalars())


def list_sku_display(
    db: Session, tenant_id: int, sku_ids: Sequence[int]
) -> list[tuple[int, str, str]]:
    """返回 ``(sku_id, sku_code, product_name)``，供库存列表 / 流水展示冗余字段。"""
    if not sku_ids:
        return []
    rows = db.execute(
        select(ProductSku.id, ProductSku.sku_code, Product.name)
        .join(Product, Product.id == ProductSku.product_id)
        .where(
            ProductSku.tenant_id == tenant_id,
            Product.tenant_id == tenant_id,
            ProductSku.id.in_(list(sku_ids)),
        )
    ).all()
    return [(int(row[0]), str(row[1]), str(row[2])) for row in rows]


def search_sku_ids(db: Session, tenant_id: int, keyword: str) -> list[int]:
    """按 SKU 编码 / SKU 名称 / 商品名称模糊搜索 SKU ID。"""
    like = f"%{keyword.strip()}%"
    rows = db.execute(
        select(ProductSku.id)
        .join(Product, Product.id == ProductSku.product_id)
        .where(
            ProductSku.tenant_id == tenant_id,
            Product.tenant_id == tenant_id,
            or_(
                ProductSku.sku_code.like(like),
                ProductSku.name.like(like),
                Product.name.like(like),
            ),
        )
        .order_by(ProductSku.id)
        .limit(_SEARCH_LIMIT)
    ).scalars()
    return [int(item) for item in rows]


__all__ = [
    "list_products",
    "get_product",
    "get_product_by_code",
    "list_skus",
    "count_skus_by_products",
    "get_sku",
    "get_sku_by_code",
    "list_images",
    "delete_images",
    "list_sku_display",
    "search_sku_ids",
]
