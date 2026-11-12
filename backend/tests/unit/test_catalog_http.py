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
    from app.modules.storage import models_center  # noqa: F401 - fixture 独立运行时注册云存储表
    from app.core.config import settings
    engines=[create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool) for _ in range(2)]
    _sqlite_metadata(TenantBase.metadata,("products","product_skus","product_images","sku_identifiers","suppliers","catalog_options","audit_logs","files")).create_all(engines[0])
    _sqlite_metadata(CenterBase.metadata,("users","tenant_memberships","storage_profiles","platform_system_settings","platform_audit_logs")).create_all(engines[1])
    db,center=[Session(e,expire_on_commit=False) for e in engines]
    center.add_all([User(id=9,email="one@example.test",display_name="张三"),User(id=10,email="two@example.test",display_name="李四")])
    center.add_all([TenantMembership(id=11,tenant_id=1,user_id=9,member_no=1,status="active"),TenantMembership(id=22,tenant_id=2,user_id=10,member_no=1,status="active")])
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


def test_image_upload_persistence_and_tenant_isolation(api,monkeypatch):
    client,_,db=api
    from app.modules.storage import service as storage
    from app.modules.audit.models_tenant import FileRecord
    def upload(center,tenant_id,*,object_key,data,content_type):
        assert tenant_id==1 and content_type=="image/png" and data.startswith(b"\x89PNG")
        return SimpleNamespace(profile_id=5,provider="oss",object_key=object_key,url="https://cdn.example/"+object_key)
    monkeypatch.setattr(storage,"upload_default",upload)
    png=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")
    response=client.post(BASE+"/media",files={"file":("image.png",png,"image/png")})
    assert response.status_code==200,response.text
    payload=response.json()["data"]
    assert payload["url"].startswith("https://cdn.example/catalog/1/")
    stored=db.get(FileRecord,int(payload["id"]))
    assert stored.url==payload["url"] and stored.storage_profile_id==5 and stored.storage_provider=="oss"
    response=client.get(BASE+"/media/"+payload["id"],follow_redirects=False)
    assert response.status_code==307 and response.headers["location"]==payload["url"]
    with pytest.raises(AppError):
        resources.image_file(db,2,int(payload["id"]))
    response=client.post(BASE,json={"code":"BAD-IMAGE","name":"跨租户图片","images":[payload["url"].replace("/catalog/1/","/catalog/2/")],"skus":[{"sku_code":"BAD-IMAGE"}]})
    assert response.status_code==422
    response=client.post(BASE,json={"code":"GOOD-IMAGE","name":"云图片","images":[payload["url"]],"skus":[{"sku_code":"GOOD-IMAGE","image_url":payload["url"]}]})
    assert response.status_code==200,response.text
    assert response.json()["data"]["images"][0]["url"]==payload["url"]
    assert client.post(BASE+"/media",files={"file":("attack.svg",b"<svg/>","image/svg+xml")}).status_code==422


def test_missing_cloud_config_and_legacy_local_images(api):
    from app.core.config import settings
    from app.modules.audit.models_tenant import FileRecord
    from pathlib import Path
    client,_,db=api
    png=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")
    # 中心库补齐配置表，但不配置默认厂商；不得悄悄回写本地。
    from app.modules.storage.models_center import SystemSettings,StorageProfile
    from app.api.deps import get_center_db
    center=client.app.dependency_overrides[get_center_db]()
    _sqlite_metadata(CenterBase.metadata,(SystemSettings.__tablename__,StorageProfile.__tablename__)).create_all(center.get_bind())
    assert client.post(BASE+"/media",files={"file":("image.png",png,"image/png")}).status_code==422
    key="catalog/1/legacy.png"
    path=Path(settings.catalog_media_dir)/key
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(png)
    row=FileRecord(tenant_id=1,object_key=key,filename="legacy.png",content_type="image/png",size_bytes=len(png),status="active")
    db.add(row);db.commit()
    response=client.get(BASE+f"/media/{row.id}")
    assert response.status_code==200 and response.content==png


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


def test_default_vendor_switch_keeps_existing_database_urls(api,monkeypatch):
    from app.modules.storage import service as storage,providers
    from app.modules.storage.schemas import StorageProfileInput
    from app.modules.audit.models_tenant import FileRecord
    from app.api.deps import get_center_db
    client,_,db=api
    center=client.app.dependency_overrides[get_center_db]()
    seen=[]
    class FakeClient:
        def __init__(self,config): self.config=config
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def put_object(self,key,data,content_type):
            seen.append((self.config.provider,self.config.bucket,key))
            return providers.UploadResult(self.config.profile_id,self.config.provider,key,self.config.public_base_url+"/"+key)
    monkeypatch.setattr(providers,"create_client",FakeClient)
    def profile(name,provider,url):
        return storage.save_profile(center,StorageProfileInput(name=name,provider=provider,endpoint="https://cloud.example",bucket="erp-images",public_base_url=url,
            access_key_id="example-ak",secret_access_key="example-sk"),actor_user_id=9)
    first=profile("OSS","oss","https://first.example")
    storage.set_default(center,int(first["id"]),version=1,actor_user_id=9)
    png=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")
    old=client.post(BASE+"/media",files={"file":("image.png",png,"image/png")}).json()["data"]
    second=profile("COS","cos","https://second.example")
    storage.set_default(center,int(second["id"]),version=2,actor_user_id=9)
    new=client.post(BASE+"/media",files={"file":("image.png",png,"image/png")}).json()["data"]
    assert old["url"].startswith("https://first.example/") and new["url"].startswith("https://second.example/")
    assert db.get(FileRecord,int(old["id"])).url==old["url"]
    assert [item[0] for item in seen]==["oss","cos"]
    assert client.get(BASE+"/media/"+old["id"],follow_redirects=False).headers["location"]==old["url"]
