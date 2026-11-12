"""基础资料维护的字段、租户隔离、引用和权限边界；不启动服务。"""
from sqlalchemy import select, func

from app.core.access import EffectivePermissions, ScopeGrant
from app.modules.audit.models_tenant import AuditLog
from app.modules.catalog.models_tenant import Supplier, CatalogOption
from tests.unit.test_catalog_http import api, BASE  # noqa: F401 - 复用进程内 HTTP fixture


def test_supplier_optional_contacts_update_and_audit(api):
    client,_,db=api
    assert client.get(BASE+"/options").json()["data"]["can_manage_options"] is True
    supplier=client.post(BASE+"/suppliers",json={"code":"S-1","name":"供应商"}).json()["data"]
    assert all(supplier[key] is None for key in ("contact_name","contact_details","remark"))
    response=client.patch(BASE+"/suppliers/"+supplier["id"],json={"contact_name":" 张三 ","contact_details":"微信/电话/邮箱","remark":"可选备注"})
    assert response.status_code==200,response.text
    assert response.json()["data"]["contact_name"]=="张三"
    changes=db.scalars(select(AuditLog).where(AuditLog.action=="catalog.option.update")).one().payload["changes"]
    assert {item["field"] for item in changes}=={"contact_name","contact_details","remark"}
    assert all(item["old"] is None for item in changes)
    count=db.scalar(select(func.count()).select_from(AuditLog))
    assert client.patch(BASE+"/suppliers/"+supplier["id"],json={"contact_name":"张三"}).status_code==200
    assert db.scalar(select(func.count()).select_from(AuditLog))==count
    assert client.patch(BASE+"/suppliers/"+supplier["id"],json={"contact_name":"","contact_details":None,"status":"disabled"}).status_code==200
    listing=client.get(BASE+"/options").json()["data"]["suppliers"][0]
    assert listing["status"]=="disabled" and listing["contact_name"] is None and listing["contact_details"] is None
    assert client.patch(BASE+"/suppliers/"+supplier["id"],json={"name":None}).status_code==422
    assert client.post(BASE+"/suppliers",json={"code":"S-2","name":"另一家","contact_details":"x"*256}).status_code==422


def test_supplier_duplicate_and_cross_tenant_update(api):
    client,_,db=api
    first=client.post(BASE+"/suppliers",json={"code":"ONE","name":"一家"}).json()["data"]
    second=client.post(BASE+"/suppliers",json={"code":"TWO","name":"二家","contact_name":"李四","contact_details":"邮箱","remark":"备注"}).json()["data"]
    assert second["contact_name"]=="李四"
    assert client.patch(BASE+"/suppliers/"+second["id"],json={"code":"ONE"}).status_code==409
    assert db.get(Supplier,int(second["id"])).code=="TWO"
    other=Supplier(tenant_id=2,code="OTHER",name="别的租户")
    db.add(other);db.commit()
    assert client.patch(BASE+f"/suppliers/{other.id}",json={"name":"不应修改"}).status_code==404
    assert db.get(Supplier,int(first["id"])).name=="一家"


def test_option_reference_rename_and_category_parent_validation(api):
    client,_,db=api
    def option(kind,name,parent_id=None):
        response=client.post(BASE+"/options",json={"kind":kind,"name":name,"parent_id":parent_id})
        assert response.status_code==200,response.text
        return response.json()["data"]
    brand=option("brand","品牌")
    category=option("category","父分类")
    child=option("category","子分类",category["id"])
    tag=option("tag","新品")
    packaging=option("packaging","纸盒")
    assert client.patch(BASE+"/options/"+category["id"],json={"parent_id":child["id"]}).status_code==422
    assert client.patch(BASE+"/options/"+child["id"],json={"parent_id":brand["id"]}).status_code==422
    assert client.patch(BASE+"/options/"+brand["id"],json={"parent_id":category["id"]}).status_code==422
    other=CatalogOption(tenant_id=2,kind="category",name="别的租户")
    db.add(other);db.commit()
    assert client.patch(BASE+"/options/"+child["id"],json={"parent_id":other.id}).status_code==422
    assert client.patch(BASE+f"/options/{other.id}",json={"name":"不能改"}).status_code==404
    assert client.patch(BASE+"/options/"+brand["id"],json={"name":"品牌改名"}).status_code==200
    response=client.post(BASE,json={"code":"P-1","name":"商品","brand":"品牌改名","category":"子分类","feature_tags":["新品"],"packaging_name":"纸盒","skus":[{"sku_code":"SKU-1"}]})
    assert response.status_code==200,response.text
    for entry in (brand,child,tag,packaging):
        assert client.patch(BASE+"/options/"+entry["id"],json={"name":"不能修改"}).status_code==409
        assert client.patch(BASE+"/options/"+entry["id"],json={"status":"disabled"}).status_code==200
    assert client.patch(BASE+"/options/"+category["id"],json={"parent_id":None}).status_code==200


def test_option_mutations_require_all_scope_update(api):
    client,actor,_=api
    supplier=client.post(BASE+"/suppliers",json={"code":"SUP","name":"供应商"}).json()["data"]
    option=client.post(BASE+"/options",json={"kind":"brand","name":"品牌"}).json()["data"]
    actor.effective=EffectivePermissions(grants=(
        ScopeGrant(role_binding_id=1,role_id=1,scope_type="all",permissions=frozenset({"catalog.product.read"})),
        ScopeGrant(role_binding_id=2,role_id=2,scope_type="stores",scope_ids=frozenset({1}),permissions=frozenset({"catalog.product.update"})),
    ))
    assert client.get(BASE+"/options").json()["data"]["can_manage_options"] is False
    assert client.patch(BASE+"/suppliers/"+supplier["id"],json={"name":"不能改"}).status_code==403
    assert client.patch(BASE+"/options/"+option["id"],json={"status":"disabled"}).status_code==403
    assert client.post(BASE+"/suppliers",json={"code":"NEW","name":"不能新增"}).status_code==403
