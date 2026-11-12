"""SKU 标识唯一性、历史归属和 TikTok 别名订单幂等；不连接外部服务。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import Integer, MetaData, create_engine, select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ConflictError, ErrorCode
from app.core.security import utc_now
from app.db.base import TenantBase
from app.modules.catalog.identifiers import (
    list_aliases, normalize_identifier, resolve_identifiers, sync_identifiers,
)
from app.modules.catalog.models_tenant import Product, ProductSku, SkuIdentifier
from app.modules.channels import service as channels
from app.modules.channels.models_tenant import ChannelListing, ChannelListingLink
from app.modules.sales import service as sales
from app.modules.sales.models_tenant import OrderItem, PlatformOrderRaw
from app.modules.sales.order_mapping import parse_tiktok_order
from tests.unit.test_sales import SQLITE_TABLES, stock_stub
from tests.unit.test_tiktok_phase2 import ORDER


@pytest.fixture
def db():
    copied = MetaData()
    for name in (*SQLITE_TABLES, "products", "product_skus", "sku_identifiers",
                 "channel_listings", "channel_listing_links"):
        table = TenantBase.metadata.tables[name].to_metadata(copied)
        table.c.id.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    copied.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def seed(db, *, sku_id=1, tenant_id=1, code="LOCAL-1"):
    product = Product(id=sku_id, tenant_id=tenant_id, code=f"SPU-{sku_id}", name="商品", status="active")
    sku = ProductSku(id=sku_id, tenant_id=tenant_id, product_id=product.id,
                     sku_code=code, name="SKU", status="active")
    db.add_all([product, sku])
    db.flush()
    return sku


def test_aliases_share_namespace_with_primary_and_keep_internal_characters(db):
    first = seed(db)
    second = seed(db, sku_id=2, code="LOCAL-2")
    sync_identifiers(db, 1, first, [" TK-Black ", "TK Black"])
    assert resolve_identifiers(db, 1, ["tk-black", "TK BLACK"]) == {
        "tk-black": (1, 1), "tk black": (1, 1),
    }
    assert resolve_identifiers(db, 1, ["TKBlack"]) == {}
    with pytest.raises(ConflictError):
        sync_identifiers(db, 1, second, ["tk-BLACK"])
    with pytest.raises(ConflictError):
        sync_identifiers(db, 1, second, ["local-1"])
    assert normalize_identifier(" Straße ") == "strasse"


def test_aliases_are_tenant_isolated_and_disabled_aliases_stay_reserved(db):
    first = seed(db)
    other_tenant = seed(db, sku_id=2, tenant_id=2, code="LOCAL-1")
    third = seed(db, sku_id=3, code="LOCAL-3")
    sync_identifiers(db, 1, first, ["TK-1"])
    sync_identifiers(db, 2, other_tenant, ["TK-1"])
    sync_identifiers(db, 1, first, [])
    assert resolve_identifiers(db, 1, ["TK-1"]) == {}
    assert resolve_identifiers(db, 2, ["TK-1"]) == {"tk-1": (2, 2)}
    assert list_aliases(db, 1, [1])[1][0]["is_active"] is False
    with pytest.raises(ConflictError):
        sync_identifiers(db, 1, third, ["TK-1"])
    sync_identifiers(db, 1, first, ["TK-1"])
    assert resolve_identifiers(db, 1, ["TK-1"]) == {"tk-1": (1, 1)}


def test_renamed_primary_is_preserved_and_none_keeps_aliases(db):
    sku = seed(db)
    sync_identifiers(db, 1, sku, ["TK-1"])
    sku.sku_code = "LOCAL-NEW"
    sync_identifiers(db, 1, sku, None, previous_code="LOCAL-1")
    assert resolve_identifiers(db, 1, ["local-1", "LOCAL-NEW", "TK-1"]) == {
        "local-1": (1, 1), "local-new": (1, 1), "tk-1": (1, 1),
    }
    rows = list(db.execute(select(SkuIdentifier).where(SkuIdentifier.sku_id == 1)).scalars())
    assert [row.identifier for row in rows if row.kind == "primary"] == ["LOCAL-NEW"]


def test_alias_candidates_and_confirmed_links_are_preserved(db, monkeypatch):
    sku = seed(db)
    sync_identifiers(db, 1, sku, ["TK-Black"])
    candidates = channels.load_sku_candidates(db, 1)
    match = channels.match_listing_to_skus(external_sku_id="tk-black", listing_specs={}, candidates=candidates)
    assert match.sku_id == 1
    existing = SimpleNamespace(mapping_source="auto", link_status="mapped", sku_id=99,
                               product_id=88, note="先前已确认")
    monkeypatch.setattr(channels.repo, "get_link_by_listing", lambda *args: existing)
    result = channels._match_and_save_link(db, 1, SimpleNamespace(id=7), candidates)
    assert (result.sku_id, result.product_id) == (99, 88)


def test_tiktok_orders_resolve_alias_then_keep_historical_mapping(db, monkeypatch, stock_stub):
    sku = seed(db)
    sync_identifiers(db, 1, sku, ["SKU-1"])
    payload = deepcopy(ORDER)
    raw = PlatformOrderRaw(tenant_id=1, store_id=10, channel="tiktok", external_order_no=payload["id"],
                           payload=payload)
    db.add(raw)
    db.flush()
    for _ in range(2):
        sales.normalize_order_from_raw(db, tenant_id=1, raw_id=raw.id)
        db.commit()
    item = db.execute(select(OrderItem)).scalar_one()
    assert item.sku_id == sku.id
    assert len(stock_stub["calls"]) == 1
    sync_identifiers(db, 1, sku, [])
    sales.normalize_order_from_raw(db, tenant_id=1, raw_id=raw.id)
    db.commit()
    assert item.sku_id == sku.id
    assert len(stock_stub["calls"]) == 1


def test_native_tiktok_id_is_not_treated_as_an_alias(db, monkeypatch, stock_stub):
    sku = seed(db)
    sync_identifiers(db, 1, sku, ["1729000000000000002"])
    payload = deepcopy(ORDER)
    payload["line_items"][0].pop("seller_sku")
    parsed = parse_tiktok_order(payload)
    assert parsed.lines[0].seller_sku == ""
    assert parsed.lines[0].platform_sku_id == "1729000000000000002"
    raw = PlatformOrderRaw(tenant_id=1, store_id=10, channel="tiktok", external_order_no=payload["id"],
                           payload=payload)
    db.add(raw)
    db.flush()
    sales.normalize_order_from_raw(db, tenant_id=1, raw_id=raw.id)
    assert db.execute(select(OrderItem)).scalar_one().sku_id is None
    assert stock_stub["calls"] == []


def test_native_id_preserves_link_when_seller_sku_changes(db):
    sku = seed(db)
    listing = ChannelListing(id=7, tenant_id=1, store_id=10, external_item_id="PRODUCT-1",
        external_sku_id="OLD-SELLER-CODE", title="商品", currency="USD", status="active",
        raw={"channel": "tiktok", "sku": {"id": "NATIVE-1", "seller_sku": "OLD-SELLER-CODE"}})
    link = ChannelListingLink(id=7, tenant_id=1, listing_id=7, product_id=sku.product_id,
                              sku_id=sku.id, link_status="mapped", mapping_source="auto")
    db.add_all([listing, link])
    db.flush()
    remote = channels.parse_tiktok_listings([{"id": "PRODUCT-1", "title": "新标题", "status": "ACTIVATE",
                  "skus": [{"id": "NATIVE-1", "seller_sku": "NEW-SELLER-CODE"}]}])[0]
    updated, created = channels._upsert_listing_row(db, 1, 10, remote, utc_now())
    assert not created and updated.id == 7
    assert updated.external_sku_id == "NEW-SELLER-CODE"
    assert channels._match_and_save_link(db, 1, updated, []).sku_id == sku.id
    assert channels.resolve_listings_sku(db, 1, 10, [("PRODUCT-1", "NATIVE-1")]) == {
        ("PRODUCT-1", "NATIVE-1"): (sku.id, sku.product_id),
    }


def test_stock_api_requires_native_id_instead_of_using_seller_code(db, monkeypatch):
    class Client:
        def close(self):
            pass

        def update_inventory(self, *args):
            pytest.fail("不允许将商家编码作为平台 SKU ID 请求平台")

    monkeypatch.setattr(channels, "build_tiktok_client", lambda *a: Client())
    listing = SimpleNamespace(external_item_id="PRODUCT-1", external_sku_id="SELLER-CODE",
                              raw={"sku": {"seller_sku": "SELLER-CODE"}})
    with pytest.raises(AppError) as error:
        channels._push_tiktok_stock(None, db, 1, SimpleNamespace(warehouse_id="WH-1"), [listing], 2)
    assert error.value.code == ErrorCode.SKU_MAPPING_MISSING


@pytest.mark.parametrize("first_missing_seller", [True, False])
def test_order_seller_code_enrichment_or_rename_keeps_one_item_and_stock_effect(db, stock_stub, first_missing_seller):
    sku = seed(db)
    sync_identifiers(db, 1, sku, ["SKU-1", "NEW-ALIAS"])
    db.add(ChannelListing(id=7, tenant_id=1, store_id=10,
        external_item_id="1729000000000000001", external_sku_id="SKU-1", title="商品",
        raw={"channel": "tiktok", "sku": {"id": "1729000000000000002", "seller_sku": "SKU-1"}}))
    db.add(ChannelListingLink(id=7, tenant_id=1, listing_id=7, product_id=sku.product_id,
                             sku_id=sku.id, link_status="mapped", mapping_source="auto"))
    first = deepcopy(ORDER)
    if first_missing_seller:
        first["line_items"][0].pop("seller_sku")
    raw = PlatformOrderRaw(tenant_id=1, store_id=10, channel="tiktok", external_order_no=first["id"], payload=first)
    db.add(raw)
    db.flush()
    sales.normalize_order_from_raw(db, tenant_id=1, raw_id=raw.id)
    db.commit()
    original_key = db.execute(select(OrderItem)).scalar_one().external_sku_id
    updated = deepcopy(ORDER)
    updated["line_items"][0]["seller_sku"] = "NEW-ALIAS"
    raw.payload = updated
    sales.normalize_order_from_raw(db, tenant_id=1, raw_id=raw.id)
    db.commit()
    items = list(db.execute(select(OrderItem)).scalars())
    assert len(items) == 1
    assert items[0].external_sku_id == original_key
    assert items[0].platform_sku_id == "1729000000000000002"
    assert len(stock_stub["calls"]) == 1


def test_legacy_native_key_is_backfilled_and_ambiguous_legacy_lines_are_blocked(db, stock_stub):
    sku = seed(db)
    sync_identifiers(db, 1, sku, ["SKU-1", "NEW-ALIAS"])
    # 模拟已存在的旧版本订单行：键为平台ID，但当时没有单独的平台ID列。
    order = sales.Order(tenant_id=1, store_id=10, channel="tiktok", external_order_no=ORDER["id"],
                        status="paid", platform_status="AWAITING_SHIPMENT", currency="USD")
    db.add(order)
    db.flush()
    old = OrderItem(tenant_id=1, order_id=order.id, external_item_id="1729000000000000001",
                    external_sku_id="1729000000000000002", platform_sku_id="", sku_id=sku.id,
                    item_name="商品", quantity=2, unit_price=1, discount_amount=0, mapping_status="mapped")
    db.add(old)
    db.flush()
    parsed = parse_tiktok_order(deepcopy(ORDER))
    rows = sales._upsert_order_items(db, tenant_id=1, order=order, parsed=parsed,
        mapping={(parsed.lines[0].external_item_id, parsed.lines[0].external_sku_id): (sku.id, "mapped")})
    assert rows[0].id == old.id
    assert rows[0].platform_sku_id == "1729000000000000002"
    # 若旧库同时已有另一条相同nativeID的历史行，不能任选其一继续扣减。
    db.add(OrderItem(tenant_id=1, order_id=order.id, external_item_id="1729000000000000001",
        external_sku_id="SKU-1", platform_sku_id=old.platform_sku_id, sku_id=sku.id,
        item_name="重复历史行", quantity=2, unit_price=1, discount_amount=0, mapping_status="mapped"))
    db.flush()
    with pytest.raises(AppError) as error:
        sales._upsert_order_items(db, tenant_id=1, order=order, parsed=parsed, mapping={})
    assert error.value.code == ErrorCode.SKU_MAPPING_NOT_UNIQUE
    assert stock_stub["calls"] == []


def test_same_native_sku_with_different_seller_codes_is_merged():
    payload = deepcopy(ORDER)
    second = deepcopy(payload["line_items"][0])
    second["seller_sku"] = "NEW-ALIAS"
    payload["line_items"].append(second)
    parsed = parse_tiktok_order(payload)
    assert len(parsed.lines) == 1 and parsed.lines[0].quantity == 4


def test_ingest_marks_ambiguous_legacy_order_without_creating_or_deducting(db, stock_stub):
    sku = seed(db)
    sync_identifiers(db, 1, sku, ["SKU-1"])
    order = sales.Order(tenant_id=1, store_id=10, channel="tiktok", external_order_no=ORDER["id"],
                        status="pending", platform_status="UNPAID", currency="USD")
    db.add(order)
    db.flush()
    db.add(OrderItem(tenant_id=1, order_id=order.id, external_item_id="1729000000000000001",
        external_sku_id="OLD-UNKNOWN-ALIAS", platform_sku_id="", sku_id=sku.id,
        item_name="旧订单行", quantity=2, unit_price=1, discount_amount=0, mapping_status="mapped"))
    db.commit()
    result = sales.ingest_platform_orders(db, tenant_id=1, store_id=10, channel="tiktok", payloads=[deepcopy(ORDER)])
    assert result["errors"] == 1
    db.refresh(order)
    assert order.status == "pending" and "歧义" in order.abnormal_reason
    assert len(list(db.execute(select(OrderItem)).scalars())) == 1
    assert stock_stub["calls"] == []
