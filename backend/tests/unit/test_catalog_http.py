"""商品路由与文件边界回归；仅进程内HTTP和临时数据库，不启动服务。"""
import base64
from decimal import Decimal
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import deps
from app.core.access import EffectivePermissions, ScopeGrant
from app.core.errors import AppError
from app.core.permissions import ALL_PERMISSIONS
from app.db.base import TenantBase, CenterBase
from app.modules.catalog.router import router
from app.modules.catalog import resources
from app.modules.identity.models_center import User, TenantMembership
from tests.unit.test_identity import _sqlite_metadata


@pytest.fixture
def api(monkeypatch,tmp_path):
    from app.modules.platform import quotas
    from app.core.config import settings
    engines=[create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool) for _ in range(2)]
    _sqlite_metadata(TenantBase.metadata,("products","product_skus","product_images","sku_identifiers","suppliers","catalog_options","audit_logs","files")).create_all(engines[0])
    _sqlite_metadata(CenterBase.metadata,("users","tenant_memberships")).create_all(engines[1])
    db,center=[Session(e,expire_on_commit=False) for e in engines]
    center.add_all([User(id=9,email="one@example.test",display_name="张三"),User(id=10,email="two@example.test",display_name="李四")])
    center.add_all([TenantMembership(id=11,tenant_id=1,user_id=9,status="active"),TenantMembership(id=22,tenant_id=2,user_id=10,status="active")])
    center.commit()
    actor=SimpleNamespace(effective=EffectivePermissions(grants=(ScopeGrant(role_binding_id=1,role_id=1,scope_type="all",permissions=ALL_PERMISSIONS),)))
    app=FastAPI()
    app.include_router(router,prefix="/api/v1")
    app.dependency_overrides[deps.tenant_context]=lambda:SimpleNamespace(tenant_id=1,user_id=9,actor_id=9)
    app.dependency_overrides[deps.get_tenant_db]=lambda:db
    app.dependency_overrides[deps.get_center_db]=lambda:center
    app.dependency_overrides[deps.access_snapshot]=lambda:actor
    @app.exception_handler(AppError)
    async def errors(request,exc):
        return JSONResponse(status_code=exc.status_code,content={"error":exc.to_payload()})
    monkeypatch.setattr(quotas,"ensure_tenant_capacity",lambda *a:None)
    monkeypatch.setattr(settings,"catalog_media_dir",str(tmp_path))
    with TestClient(app) as client:
        yield client,actor,db
    db.close();center.close()
    for engine in engines:
        engine.dispose()


BASE="/api/v1/tenants/1/products"


def test_http_products_filters_options_and_spu_log(api):
    client,_,_=api
    assert client.get(BASE+"/options").json()["data"]["members"][0]["id"]=="11"
    supplier=client.post(BASE+"/suppliers",json={"code":"SUP-1","name":"首选供应商"}).json()["data"]
    response=client.post(BASE,json={"code":"SPU-1","name":"中文名","name_en":"English name","purchaser_id":"11",
        "preferred_supplier_id":supplier["id"],"feature_tags":["新品"],"skus":[{"sku_code":"SKU-1","aliases":["TIKTOK-ALIAS"],"cost_amount":"12.30"}]})
    assert response.status_code==200,response.text
    product=response.json()["data"]
    assert isinstance(product["cost_summary"]["current"]["value"],str)
    assert Decimal(product["cost_summary"]["current"]["value"])==Decimal("12.30")
    response=client.get(BASE,params={"q":"TIKTOK-ALIAS","sales_status":"on_sale","tag":"新品"})
    assert response.status_code==200,response.text
    assert response.json()["data"]["total"]==1
    assert client.get(BASE,params={"tag":"新"}).json()["data"]["total"]==0
    sku=product["skus"][0]
    response=client.patch(BASE+f'/{product["id"]}/skus/{sku["id"]}',json={"version":sku["version"],"sales_status":"clearance","guidance_cost":"15"})
    assert response.status_code==200,response.text
    assert response.json()["data"]["sales_status"]=="clearance"
    logs=client.get(BASE+f'/{product["id"]}/logs').json()["data"]["items"]
    assert any(c["field"]=="guidance_cost" for c in logs[0]["changes"])
    assert client.post(BASE,json={"code":"SPU-2","name":"非法人员","purchaser_id":"22","skus":[{"sku_code":"SKU-2"}]}).status_code==422


def test_image_upload_persistence_and_tenant_isolation(api):
    client,_,db=api
    png=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")
    response=client.post(BASE+"/media",files={"file":("image.png",png,"image/png")})
    assert response.status_code==200,response.text
    payload=response.json()["data"]
    response=client.get(payload["url"])
    assert response.status_code==200 and response.content==png
    assert response.headers["cache-control"]=="private, no-store"
    with pytest.raises(AppError):
        resources.image_file(db,2,int(payload["id"]))
    response=client.post(BASE,json={"code":"BAD-IMAGE","name":"跨租户图片","images":[payload["url"].replace("/tenants/1/","/tenants/2/")],"skus":[{"sku_code":"BAD-IMAGE"}]})
    assert response.status_code==422
    assert client.post(BASE+"/media",files={"file":("attack.svg",b"<svg/>","image/svg+xml")}).status_code==422


def test_cost_capability_and_http_redaction(api):
    client,actor,_=api
    product=client.post(BASE,json={"code":"SPU-1","name":"中文名","skus":[{"sku_code":"SKU-1","cost_amount":"987.65"}]}).json()["data"]
    actor.effective=EffectivePermissions(grants=(ScopeGrant(role_binding_id=1,role_id=1,scope_type="all",permissions=frozenset({"catalog.product.read","catalog.product.update"})),
        ScopeGrant(role_binding_id=2,role_id=2,scope_type="stores",scope_ids=frozenset({1}),permissions=frozenset({"catalog.cost.read"}))))
    assert client.get(BASE+"/options").json()["data"]["can_read_cost"] is False
    detail=client.get(BASE+f'/{product["id"]}').json()["data"]
    assert "cost_summary" not in detail and "cost_amount" not in detail["skus"][0]
    logs=client.get(BASE+f'/{product["id"]}/logs').json()["data"]
    assert "987.65" not in str(logs)
