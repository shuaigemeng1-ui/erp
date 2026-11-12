"""TikTok Shop Phase 2 纯逻辑测试：商品解析、订单解析、状态映射。"""

from __future__ import annotations

from decimal import Decimal

from app.modules.channels.service import parse_tiktok_listings
from app.modules.sales.order_mapping import (
    NormalizedOrderStatus,
    normalize_platform_status,
    parse_tiktok_order,
)

PRODUCT = {
    "id": "1729000000000000001",
    "title": "Test TikTok Product",
    "status": "ACTIVATE",
    "currency": "USD",
    "skus": [
        {
            "id": "1729000000000000002",
            "seller_sku": "SKU-1",
            "price": {"sale_price": "19.90", "original_price": "29.90"},
            "inventory": [{"quantity": 7}, {"quantity": 3}],
            "sales_attributes": [
                {"name": "Color", "value_name": "Black"},
                {"name": "Size", "value_name": "M"},
            ],
        }
    ],
}

ORDER = {
    "id": "579000000000000001",
    "status": "AWAITING_SHIPMENT",
    "create_time": 1700000000,
    "currency": "USD",
    "payment": {"total_amount": "39.80", "currency": "USD"},
    "recipient_address": {"name": "Buyer"},
    "line_items": [
        {
            "id": "line-1",
            "product_id": "1729000000000000001",
            "sku_id": "1729000000000000002",
            "seller_sku": "SKU-1",
            "product_name": "Test TikTok Product",
            "quantity": 2,
            "sale_price": "19.90",
            "original_price": "29.90",
            "display_status": "AWAITING_SHIPMENT",
        }
    ],
}


def test_parse_tiktok_listings() -> None:
    rows = parse_tiktok_listings([PRODUCT])
    assert len(rows) == 1
    row = rows[0]
    assert row.external_item_id == "1729000000000000001"
    assert row.external_sku_id == "SKU-1"
    assert row.title == "Test TikTok Product"
    assert row.price == Decimal("19.90")
    assert row.remote_stock == 10
    assert row.status == "active"
    assert row.specs == {"Color": "Black", "Size": "M"}


def test_parse_tiktok_order_and_status() -> None:
    parsed = parse_tiktok_order(ORDER)
    assert parsed.external_order_no == "579000000000000001"
    assert parsed.status == NormalizedOrderStatus.PAID
    assert parsed.total_amount == Decimal("39.80")
    assert parsed.buyer_name == "Buyer"
    assert len(parsed.lines) == 1
    assert parsed.lines[0].external_item_id == "1729000000000000001"
    assert parsed.lines[0].external_sku_id == "SKU-1"
    assert parsed.lines[0].quantity == 2
    assert normalize_platform_status("tiktok", "IN_TRANSIT") == NormalizedOrderStatus.SHIPPED
    assert normalize_platform_status("tiktok", "COMPLETED") == NormalizedOrderStatus.COMPLETED
    assert normalize_platform_status("tiktok", "CANCELLED") == NormalizedOrderStatus.CANCELLED
