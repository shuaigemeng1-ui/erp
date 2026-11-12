"""SKU 主编号与别名的统一标识空间；调用方负责事务提交及商品审计。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError, ConflictError, ErrorCode
from app.modules.catalog.models_tenant import Product, ProductSku, SkuIdentifier


def normalize_identifier(value: object) -> str:
    """只处理首尾空白和大小写，不删除内部空格、连接符或其它字符。"""
    return str(value or "").strip().casefold()


def _identifier(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise AppError(ErrorCode.VALIDATION_ERROR, "SKU 编号和别名必须为字符串")
    original = value.strip()
    if not original or len(original) > 128:
        raise AppError(ErrorCode.VALIDATION_ERROR, "SKU 编号和别名长度须为 1 至 128 个字符")
    return original, normalize_identifier(original)


def sync_identifiers(
    db: Session,
    tenant_id: int,
    sku: ProductSku,
    aliases: Sequence[str] | None,
    previous_code: str | None = None,
) -> None:
    """同步主编号与有效别名，保留停用记录占位，旧编号变更为有效别名。

    ``aliases=None`` 保持已有别名；显式列表为有效别名完整集合。只 flush，不 commit。
    """
    if sku.tenant_id != tenant_id:
        raise AppError(ErrorCode.SKU_NOT_FOUND)
    if sku.id is None:
        db.flush()
    primary_text, primary_key = _identifier(sku.sku_code)
    rows = list(db.execute(select(SkuIdentifier).where(
        SkuIdentifier.tenant_id == tenant_id, SkuIdentifier.sku_id == sku.id,
    ).with_for_update()).scalars())
    by_key = {row.normalized_identifier: row for row in rows}
    desired: dict[str, tuple[str, str]] = {}
    if aliases is None:
        desired.update({row.normalized_identifier: (row.identifier, "alias")
                        for row in rows if row.kind == "alias" and row.is_active})
    else:
        if isinstance(aliases, (str, bytes)):
            raise AppError(ErrorCode.VALIDATION_ERROR, "SKU 别名必须为列表")
        for value in aliases:
            original, normalized = _identifier(value)
            desired[normalized] = (original, "alias")
    if previous_code and normalize_identifier(previous_code) != primary_key:
        old_text, old_key = _identifier(previous_code)
        desired[old_key] = (old_text, "alias")
    desired[primary_key] = (primary_text, "primary")

    occupied = list(db.execute(select(SkuIdentifier).where(
        SkuIdentifier.tenant_id == tenant_id,
        SkuIdentifier.normalized_identifier.in_(list(desired)),
    ).with_for_update()).scalars())
    if any(row.sku_id != sku.id for row in occupied):
        raise ConflictError("SKU 编号或别名已被其他 SKU 使用")

    # 兼容标识回填尚未覆盖的历史主编号；停用和归档编号同样不能被抢占。
    existing_codes = db.execute(select(ProductSku.id, ProductSku.sku_code).where(
        ProductSku.tenant_id == tenant_id, ProductSku.id != sku.id,
    )).all()
    if any(normalize_identifier(code) in desired for _, code in existing_codes):
        raise ConflictError("SKU 编号或别名与已有 SKU 主编号冲突")

    try:
        with db.begin_nested():
            for row in rows:
                row.kind = "alias"
                row.is_active = row.normalized_identifier in desired
            for normalized, (original, kind) in desired.items():
                row = by_key.get(normalized)
                if row is None:
                    row = SkuIdentifier(tenant_id=tenant_id, sku_id=sku.id,
                                        identifier=original, normalized_identifier=normalized,
                                        kind=kind, is_active=True)
                    db.add(row)
                else:
                    row.identifier, row.kind, row.is_active = original, kind, True
            db.flush()
    except IntegrityError as exc:
        raise ConflictError("SKU 编号或别名已被其他 SKU 使用") from exc


def list_aliases(db: Session, tenant_id: int, sku_ids: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
    """返回指定 SKU 的有效及停用别名，主编号不重复出现在列表。"""
    if not sku_ids:
        return {}
    rows = db.execute(select(SkuIdentifier).where(
        SkuIdentifier.tenant_id == tenant_id, SkuIdentifier.sku_id.in_(list(sku_ids)),
        SkuIdentifier.kind == "alias",
    ).order_by(SkuIdentifier.id)).scalars()
    result: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        result.setdefault(int(row.sku_id), []).append({
            "id": str(row.id), "identifier": row.identifier, "is_active": bool(row.is_active),
        })
    return result


def resolve_identifiers(
    db: Session, tenant_id: int, values: Iterable[str],
) -> dict[str, tuple[int, int]]:
    """精确解析有效主编号/别名；结果键为标准化标识，归档商品与 SKU 不参与。"""
    wanted = {normalize_identifier(value) for value in values if normalize_identifier(value)}
    if not wanted:
        return {}
    rows = db.execute(select(SkuIdentifier.normalized_identifier, ProductSku.id, ProductSku.product_id)
        .join(ProductSku, (ProductSku.id == SkuIdentifier.sku_id)
              & (ProductSku.tenant_id == SkuIdentifier.tenant_id))
        .join(Product, (Product.id == ProductSku.product_id) & (Product.tenant_id == ProductSku.tenant_id))
        .where(SkuIdentifier.tenant_id == tenant_id, SkuIdentifier.is_active.is_(True),
               SkuIdentifier.normalized_identifier.in_(wanted),
               ProductSku.status == "active", Product.status == "active")).all()
    result = {key: (int(sku_id), int(product_id)) for key, sku_id, product_id in rows}
    # 历史主编号在尚未回填标识时仍可精确识别，但不得跳过已存在的停用占位。
    missing = wanted - result.keys()
    if missing:
        reserved = set(db.execute(select(SkuIdentifier.normalized_identifier).where(
            SkuIdentifier.tenant_id == tenant_id,
            SkuIdentifier.normalized_identifier.in_(missing),
        )).scalars())
        if missing - reserved:
            legacy = db.execute(select(ProductSku.id, ProductSku.product_id, ProductSku.sku_code)
                .join(Product, (Product.id == ProductSku.product_id) & (Product.tenant_id == ProductSku.tenant_id))
                .where(ProductSku.tenant_id == tenant_id, ProductSku.status == "active",
                       Product.status == "active")).all()
            groups: dict[str, list[tuple[int, int]]] = {}
            for sku_id, product_id, code in legacy:
                key = normalize_identifier(code)
                if key in missing - reserved:
                    groups.setdefault(key, []).append((int(sku_id), int(product_id)))
            result.update({key: matches[0] for key, matches in groups.items() if len(matches) == 1})
    return result
