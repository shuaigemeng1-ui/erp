"""平台存储配置与访问边界，只使用临时数据库和模拟云客户端。"""
from types import SimpleNamespace
import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import deps
from app.core.auth import AuthenticatedUser
from app.core.errors import AppError, ConflictError
from app.core.security import decrypt_secret
from app.db.base import CenterBase
from app.modules.platform.models_center import PlatformAuditLog
from app.modules.storage import service
from app.modules.storage.models_center import StorageProfile, SystemSettings
from app.modules.storage.router import router
from app.modules.storage.schemas import StorageProfileInput, StorageProfileUpdate
from tests.unit.test_identity import _sqlite_metadata


@pytest.fixture
def center():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    _sqlite_metadata(CenterBase.metadata, ("storage_profiles", "platform_system_settings", "platform_audit_logs")).create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(SystemSettings(id=1, version=1))
        db.commit()
        yield db
    engine.dispose()


def body(**updates):
    values = dict(name="主图片存储", provider="oss", endpoint="https://s3.oss-cn-hangzhou.aliyuncs.com",
        bucket="erp-images", public_base_url="https://images.example.test", key_prefix="erp",
        access_key_id="example-access-key", secret_access_key="example-secret-key")
    values.update(updates)
    return StorageProfileInput(**values)


def create(center, **updates):
    return service.save_profile(center, body(**updates), actor_user_id=7)


def test_credentials_encrypted_and_absent_from_responses_and_audit(center):
    payload = create(center)
    assert payload["has_access_key"] and payload["has_secret_key"]
    assert payload["region"] == "cn-hangzhou"
    row = center.get(StorageProfile, int(payload["id"]))
    assert row.access_key_id_cipher != "example-access-key"
    assert decrypt_secret(row.secret_access_key_cipher) == "example-secret-key"
    output = json.dumps(service.get_settings(center), ensure_ascii=False)
    audits = json.dumps([row.payload for row in center.execute(select(PlatformAuditLog)).scalars()])
    for secret in ("example-access-key", "example-secret-key", row.access_key_id_cipher, row.secret_access_key_cipher):
        assert secret not in output and secret not in audits


def test_empty_keys_preserved_provider_change_requires_full_keys(center):
    first = create(center)
    update = body(name="更新名称", access_key_id="  ", secret_access_key="")
    saved = service.save_profile(center, StorageProfileUpdate(**update.model_dump(), version=first["version"]), actor_user_id=7, profile_id=int(first["id"]))
    assert saved["version"] == first["version"] + 1
    row = center.get(StorageProfile, int(first["id"]))
    assert decrypt_secret(row.access_key_id_cipher) == "example-access-key"
    with pytest.raises(AppError) as error:
        service.save_profile(center, StorageProfileUpdate(**body(provider="s3",access_key_id=None,secret_access_key=None).model_dump(),version=saved["version"]), actor_user_id=7,profile_id=row.id)
    assert error.value.status_code == 422
    assert center.get(StorageProfile,row.id).provider == "oss"


def test_default_lock_version_and_default_account_cannot_be_disabled(center):
    first = create(center)
    result = service.set_default(center,int(first["id"]),version=1,actor_user_id=7)
    assert result["default_storage_profile_id"] == first["id"]
    assert result["version"] == 2 and result["profiles"] and result["providers"]
    with pytest.raises(ConflictError):
        service.set_default(center,int(first["id"]),version=1,actor_user_id=7)
    with pytest.raises(ConflictError):
        service.save_profile(center,StorageProfileUpdate(**body(enabled=False).model_dump(),version=first["version"]),actor_user_id=7,profile_id=int(first["id"]))
    assert center.get(StorageProfile,int(first["id"])).enabled is True
    second = create(center,name="备用云存储",provider="s3_compatible",public_base_url="https://new-images.example.test")
    result = service.set_default(center,int(second["id"]),version=2,actor_user_id=7)
    assert result["default_storage_profile_id"] == second["id"]
    assert center.get(StorageProfile,int(first["id"])).public_base_url == "https://images.example.test"


def test_stale_profile_update_is_rejected(center):
    first = create(center)
    service.save_profile(center,StorageProfileUpdate(**body(name="管理员一").model_dump(),version=1),actor_user_id=7,profile_id=int(first["id"]))
    with pytest.raises(ConflictError):
        service.save_profile(center,StorageProfileUpdate(**body(name="管理员二").model_dump(),version=1),actor_user_id=7,profile_id=int(first["id"]))
    assert center.get(StorageProfile,int(first["id"])).name == "管理员一"


def test_no_default_upload_fails_without_cloud_request(center,monkeypatch):
    from app.modules.storage import providers
    monkeypatch.setattr(providers,"create_client",lambda config: pytest.fail("must not call cloud without default config"))
    with pytest.raises(AppError) as error:
        service.upload_default(center,1,object_key="catalog/1/test.png",data=b"data",content_type="image/png")
    assert error.value.status_code == 422


def test_upload_uses_current_config_and_sanitizes_provider_errors(center,monkeypatch):
    from app.modules.storage import providers
    first = create(center)
    service.set_default(center,int(first["id"]),version=1,actor_user_id=7)
    calls=[]
    class Client:
        def __init__(self,config): self.config=config
        def __enter__(self): return self
        def __exit__(self,*args): calls.append("closed")
        def put_object(self,key,data,content_type):
            calls.append((self.config.profile_id,key,content_type))
            return providers.UploadResult(self.config.profile_id,self.config.provider,"erp/"+key,"https://images.example.test/erp/"+key)
        def head_bucket(self): raise RuntimeError("SDK failure includes example-secret-key")
    monkeypatch.setattr(providers,"create_client",Client)
    result=service.upload_default(center,1,object_key="catalog/1/test.png",data=b"data",content_type="image/png")
    assert result.object_key=="erp/catalog/1/test.png" and result.url.endswith(result.object_key)
    assert calls[-1]=="closed"
    with pytest.raises(AppError) as error:
        service.test_profile(center,int(first["id"]))
    assert "example-secret-key" not in str(error.value) and error.value.status_code==503
    assert calls[-1]=="closed"


@pytest.mark.parametrize("field,value",[
    ("endpoint","http://bucket.example.test"),
    ("endpoint","https://user:secret@example.test"),
    ("endpoint","https://example.test/bucket"),
    ("public_base_url","https://example.test/?token=secret"),
    ("public_base_url","https://example.test/a/../private"),
    ("key_prefix","public/../private"),
    ("key_prefix",r"public\private"),
])
def test_invalid_endpoint_or_object_prefix_rejected(field,value):
    with pytest.raises(ValueError):
        body(**{field:value})


def test_http_routes_require_platform_admin_and_platform_scope(center):
    app=FastAPI()
    app.include_router(router,prefix="/api/v1")
    identity={"admin":True,"scope":"platform"}
    app.dependency_overrides[deps.get_center_db]=lambda:center
    app.dependency_overrides[deps.current_user]=lambda:AuthenticatedUser(user_id=7,email="admin@example.test",display_name="管理员",status="active",is_platform_admin=identity["admin"])
    @app.middleware("http")
    async def session_context(request,call_next):
        request.state.session_payload={"scope":identity["scope"]}
        return await call_next(request)
    @app.exception_handler(AppError)
    async def errors(request,exc):
        return JSONResponse(status_code=exc.status_code,content={"error":exc.to_payload()})
    with TestClient(app) as client:
        assert client.get("/api/v1/platform/system-settings").status_code==200
        identity["scope"]="tenant"
        assert client.get("/api/v1/platform/system-settings").status_code==403
        identity["scope"]="platform";identity["admin"]=False
        assert client.get("/api/v1/platform/system-settings").status_code==403
