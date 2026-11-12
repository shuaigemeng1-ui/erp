"""SPU/SKU 服务回归：使用内存库验证业务与日志同事务，不启动应用。"""
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.access import EffectivePermissions, ScopeGrant
from app.core.errors import AppError
from app.core.permissions import ALL_PERMISSIONS
from app.db.base import TenantBase
from app.modules.audit.models_tenant import AuditLog
from app.modules.catalog import service
from app.modules.catalog.identifiers import resolve_identifiers
from app.modules.catalog.models_tenant import Product, ProductImage, ProductSku
from app.modules.catalog.schemas import ProductCreate, ProductSkuUpdate, ProductUpdate
from tests.unit.test_identity import _sqlite_metadata


@pytest.fixture
def data(monkeypatch):
    from sqlalchemy import create_engine
    from app.modules.platform import quotas
    engine = create_engine("sqlite:///:memory:")
    _sqlite_metadata(TenantBase.metadata, (
        "products", "product_skus", "product_images", "sku_identifiers", "suppliers",
        "catalog_options", "audit_logs",
    )).create_all(engine)
    monkeypatch.setattr(quotas, "ensure_tenant_capacity", lambda *args: None)
    actor = EffectivePermissions(grants=(ScopeGrant(role_binding_id=1, role_id=1,
        scope_type="all", permissions=ALL_PERMISSIONS),))
    with Session(engine, expire_on_commit=False) as db:
        yield db, actor, service.AuditContext(actor_id=9, actor_name="操作员")
    engine.dispose()


def create(data, code="P1", **overrides):
    db, actor, audit = data
    values = dict(code=code, name="中文商品", name_en="Example", sales_type="variation",
        material_zh="棉", length_cm="20.1", feature_tags=["新品"],
        skus=[dict(sku_code=code+"-A", aliases=["TK-"+code], specs={"颜色": "黑色"},
            cost_amount="10.5", guidance_cost="12", average_cost="10", weight_g="50.25")])
    values.update(overrides)
    return service.create_product(db, tenant_id=1, effective=actor,
        payload=ProductCreate(**values), audit=audit)


def test_create_details_alias_search_and_summary(data):
    db, actor, audit = data
    product = create(data)
    assert product.name_en == "Example" and product.material_zh == "棉"
    assert product.length_cm == Decimal("20.1")
    assert product.skus[0].name == "中文商品"
    assert product.skus[0].price == 0
    assert product.skus[0].aliases[0].identifier == "TK-P1"
    assert product.cost_summary.current.value == Decimal("10.5")
    assert product.weight_summary.value == Decimal("50.25")
    rows, total = service.list_products(db, tenant_id=1, effective=actor, page=1, page_size=20, q="TK-P1")
    assert total == 1 and rows[0].id == product.id
    assert resolve_identifiers(db, 1, [" tk-p1 "])["tk-p1"] == (int(product.skus[0].id), int(product.id))
    log = db.execute(select(AuditLog)).scalar_one()
    assert log.product_id == int(product.id) and log.actor_name_snapshot == "操作员"


def test_sku_change_logs_old_new_and_spu_version(data):
    db, actor, audit = data
    product = create(data)
    updated = service.update_product(db, tenant_id=1, product_id=int(product.id), effective=actor,
        payload=ProductUpdate(version=product.version, skus=[ProductSkuUpdate(id=product.skus[0].id,
            version=product.skus[0].version, cost_amount="20", specs={"颜色": "白色"}, sales_status="clearance")]), audit=audit)
    assert updated.version == product.version+1
    assert updated.skus[0].version == product.skus[0].version+1
    assert updated.sales_status == "clearance" and updated.cost_amount == 20
    items, total = service.product_logs(db, tenant_id=1, product_id=int(product.id), effective=actor, page=1, page_size=20)
    changes = {c["field"]: c for c in items[0]["changes"] if c["entity"] == "sku"}
    assert changes["cost_amount"]["old"] == "10.5" and changes["cost_amount"]["new"] == "20"
    assert changes["specs.颜色"]["old"] == "黑色" and changes["specs.颜色"]["new"] == "白色"
    assert items[0]["created_at"].endswith("Z") and items[0]["actor_name"] == "操作员"
    assert total == 2


def test_unchanged_save_and_stale_version(data):
    db, actor, audit = data
    product = create(data)
    same = service.update_product(db, tenant_id=1, product_id=int(product.id), effective=actor,
        payload=ProductUpdate(version=product.version, name=product.name), audit=audit)
    assert same.version == product.version
    assert db.query(AuditLog).count() == 1
    with pytest.raises(AppError):
        service.update_product(db, tenant_id=1, product_id=int(product.id), effective=actor,
            payload=ProductUpdate(version=product.version+1, name="错误覆盖"), audit=audit)
    assert db.get(Product, int(product.id)).name == product.name
    assert db.query(AuditLog).count() == 1


def test_alias_conflict_rolls_back_all_product_changes_and_rename_keeps_old(data):
    db, actor, audit = data
    first = create(data)
    second = create(data, "P2")
    with pytest.raises(AppError):
        service.update_product(db, tenant_id=1, product_id=int(second.id), effective=actor,
            payload=ProductUpdate(name="不应保存", skus=[ProductSkuUpdate(id=second.skus[0].id, aliases=["TK-P1"])]), audit=audit)
    assert db.get(Product, int(second.id)).name == second.name
    updated = service.update_product(db, tenant_id=1, product_id=int(first.id), effective=actor,
        payload=ProductUpdate(skus=[ProductSkuUpdate(id=first.skus[0].id, sku_code="NEW-CODE")]), audit=audit)
    assert any(a.identifier == "P1-A" and a.is_active for a in updated.skus[0].aliases)
    resolved = resolve_identifiers(db, 1, ["P1-A", "NEW-CODE"])
    assert resolved["p1-a"] == resolved["new-code"]


def test_cost_permissions_and_log_redaction(data):
    db, _, audit = data
    product = create(data)
    reader = EffectivePermissions(grants=(ScopeGrant(role_binding_id=2, role_id=2, scope_type="all",
        permissions=frozenset({"catalog.product.read", "catalog.product.update"})),))
    detail = service.get_product_detail(db, tenant_id=1, product_id=int(product.id), effective=reader)
    output = detail.model_dump(mode="json")
    assert "cost_summary" not in output and "cost_amount" not in output["skus"][0]
    assert "guidance_cost" not in output["skus"][0]
    logs, _ = service.product_logs(db, tenant_id=1, product_id=int(product.id), effective=reader, page=1, page_size=20)
    assert all(not change["sensitive"] for row in logs for change in row["changes"])
    with pytest.raises(AppError):
        service.update_product(db, tenant_id=1, product_id=int(product.id), effective=reader,
            payload=ProductUpdate(skus=[ProductSkuUpdate(id=product.skus[0].id, average_cost="999")]), audit=audit)


def test_all_skus_archived_rejected_and_spu_cost_is_read_only(data):
    db, actor, audit = data
    product = create(data)
    with pytest.raises(AppError):
        service.update_product(db, tenant_id=1, product_id=int(product.id), effective=actor,
            payload=ProductUpdate(skus=[ProductSkuUpdate(id=product.skus[0].id, status="archived")]), audit=audit)
    assert db.get(ProductSku, int(product.skus[0].id)).status == "active"
    with pytest.raises(AppError):
        service.update_product(db, tenant_id=1, product_id=int(product.id), effective=actor,
            payload=ProductUpdate(cost_amount="888"), audit=audit)


def test_spu_image_replacement_preserves_sku_images(data):
    db, actor, audit = data
    product = create(data, images=["/first.png"])
    db.add(ProductImage(tenant_id=1, product_id=int(product.id), sku_id=int(product.skus[0].id), url="/sku.png", sort_order=0))
    db.commit()
    service.update_product(db, tenant_id=1, product_id=int(product.id), effective=actor,
        payload=ProductUpdate(images=["/second.png"]), audit=audit)
    images = list(db.execute(select(ProductImage).order_by(ProductImage.id)).scalars())
    assert sorted(i.url for i in images) == ["/second.png", "/sku.png"]


def test_disabled_supplier_can_be_preserved_but_not_newly_selected(data):
    from app.modules.catalog.models_tenant import Supplier
    db, actor, audit = data
    supplier = Supplier(tenant_id=1, code="OLD", name="历史供应商", status="active")
    db.add(supplier)
    db.commit()
    product = create(data, preferred_supplier_id=supplier.id)
    supplier.status = "disabled"
    db.commit()
    updated = service.update_product(db, tenant_id=1, product_id=int(product.id), effective=actor,
        payload=ProductUpdate(preferred_supplier_id=supplier.id, remark="修改其他资料"), audit=audit)
    assert updated.preferred_supplier_id == str(supplier.id)
    assert updated.remark == "修改其他资料"
    with pytest.raises(AppError, match="供应商"):
        create(data, "P2", preferred_supplier_id=supplier.id)
    other = create(data, "P3")
    with pytest.raises(AppError, match="供应商"):
        service.update_product(db, tenant_id=1, product_id=int(other.id), effective=actor,
            payload=ProductUpdate(preferred_supplier_id=supplier.id), audit=audit)


def test_disabled_dictionary_selections_preserve_history_and_reject_new_usage(data):
    from app.modules.catalog.models_tenant import CatalogOption
    db, actor, audit = data
    values = dict(brand="旧品牌", category="旧分类", packaging_name="旧包材", feature_tags=["新品"])
    product = create(data, **values)
    for field_name, kind in (("brand", "brand"), ("category", "category"),
                              ("packaging_name", "packaging"), ("feature_tags", "tag")):
        name = values[field_name][0] if kind == "tag" else values[field_name]
        db.add(CatalogOption(tenant_id=1, kind=kind, name=name, status="disabled"))
    db.commit()
    updated = service.update_product(db, tenant_id=1, product_id=int(product.id), effective=actor,
        payload=ProductUpdate(**values, remark="历史值仍可保存"), audit=audit)
    assert updated.remark == "历史值仍可保存"
    for index, (field_name, value) in enumerate(values.items()):
        with pytest.raises(AppError, match="已停用"):
            create(data, f"NEW-{index}", **{field_name: value})
    # 增加其他标签不能因保留历史停用标签而失败；另一租户同名停用项不影响本租户。
    db.add(CatalogOption(tenant_id=2, kind="tag", name="新增标签", status="disabled"))
    db.commit()
    updated = service.update_product(db, tenant_id=1, product_id=int(product.id), effective=actor,
        payload=ProductUpdate(feature_tags=["新品", "新增标签"]), audit=audit)
    assert updated.feature_tags == ["新品", "新增标签"]
