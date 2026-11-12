"""SPU 销售状态、成本和重量汇总；未归档 SKU 是唯一事实来源。"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, Mapping) else getattr(item, name, default)


def aggregate_value(values: Iterable[Decimal | None]) -> dict[str, Any]:
    """不同值、缺失与零分别处理，不将部分填写的值误显示为统一值。"""
    items = list(values)
    present = {Decimal(str(value)) for value in items if value is not None}
    missing = not items or any(value is None for value in items)
    if not present:
        return {"state": "missing", "value": None, "has_missing": True}
    if len(present) > 1:
        return {"state": "multiple", "value": None, "has_missing": missing}
    if missing:
        return {"state": "partial_missing", "value": None, "has_missing": True}
    return {"state": "uniform", "value": next(iter(present)), "has_missing": False}


def aggregate_sales_status(skus: Iterable[Any]) -> str:
    """一致时原状态；混合时部分清仓优先于部分停售，再归为在售。"""
    statuses = {
        _value(sku, "sales_status") or "on_sale"
        for sku in skus if _value(sku, "status", "active") != "archived"
    }
    if not statuses:
        return "discontinued"
    if len(statuses) == 1:
        return next(iter(statuses))
    if "clearance" in statuses:
        return "partial_clearance"
    if "discontinued" in statuses:
        return "partial_discontinued"
    return "on_sale"


def aggregate_product(skus: Sequence[Any], *, include_cost: bool = True) -> dict[str, Any]:
    """生成 API 汇总字段；无成本权限时不读取成本属性。"""
    active = [sku for sku in skus if _value(sku, "status", "active") != "archived"]
    result = {
        "sales_status": aggregate_sales_status(active),
        "weight_summary": aggregate_value(_value(sku, "weight_g") for sku in active),
    }
    if include_cost:
        result["cost_summary"] = {
            key: aggregate_value(_value(sku, column) for sku in active)
            for key, column in (("current", "cost_amount"), ("guidance", "guidance_cost"), ("average", "average_cost"))
        }
    return result
