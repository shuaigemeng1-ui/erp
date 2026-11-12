"""授权网关真实状态流：短期 state、撤销、成员停用和外部店铺身份保护。"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import Integer, MetaData

from app.core.errors import AppError
from app.modules.channels import authorization
from app.modules.channels.models_center import AuthorizationLink, ChannelAccountRegistry, PlatformChannelApp
from app.modules.channels.models_tenant import Store, StoreCredential
from app.core.security import encrypt_secret
from app.modules.identity.models_center import TenantMembership
from tests.unit.test_identity import _build_sqlite_sessions, _seed_tenant


class NonceStore:
    def __init__(self):
        self.values = {}

    def set(self, key, value, **kwargs):
        self.values[key] = value

    def getdel(self, key):
        return self.values.pop(key, None)


@pytest.fixture()
def nonce_store(monkeypatch):
    cache = NonceStore()
    monkeypatch.setattr(authorization, "cache_redis", lambda: cache)
    return cache


def _link(channel="tiktok"):
    return SimpleNamespace(id=77, channel=channel, context={
        "tenant_id": 1, "member_id": 100, "environment": "production",
        "store_id": 9, "redirect_path": "/tenants/1/stores",
    })


def test_gateway_state_is_channel_bound_and_consumed_once(nonce_store):
    state = authorization.create_oauth_state(_link())
    with pytest.raises(AppError):
        authorization.consume_state(state, "shopee")
    payload = authorization.consume_state(state, "tiktok")
    assert payload["store_id"] == 9 and payload["authorization_link_id"] == 77
    with pytest.raises(AppError):
        authorization.consume_state(state, "tiktok")


def test_gateway_state_expires_even_if_nonce_storage_has_not_evicted(nonce_store, monkeypatch):
    now = datetime(2026, 11, 12, 9)
    monkeypatch.setattr(authorization, "utc_now", lambda: now)
    state = authorization.create_oauth_state(_link())
    monkeypatch.setattr(authorization, "utc_now", lambda: now + timedelta(seconds=601))
    with pytest.raises(AppError):
        authorization.consume_state(state, "tiktok")


@pytest.fixture()
def authorization_db(monkeypatch):
    from app.db import session as sessions
    from app.modules.access import service as access
    center_factory, tenant_factory, *_ = _build_sqlite_sessions()
    center, tenant = center_factory(), tenant_factory()
    _seed_tenant(center, tenant)
    for model, bind in ((AuthorizationLink, center.get_bind()),
                        (ChannelAccountRegistry, center.get_bind()),
                        (PlatformChannelApp, center.get_bind()),
                        (Store, tenant.get_bind()), (StoreCredential, tenant.get_bind())):
        meta = MetaData()
        table = model.__table__.to_metadata(meta)
        table.c.id.type = Integer()
        meta.create_all(bind)
    center.add(PlatformChannelApp(channel="tiktok", environment="production", app_id="offline-service",
                                  app_key="offline-key", app_secret_cipher=encrypt_secret("offline-secret"),
                                  redirect_url="https://example.test/oauth/callback", status="active", region="ROW"))
    center.commit()
    tenant.add(Store(id=9, tenant_id=1, channel="tiktok", environment="production",
                     store_code="TK9", external_store_id="remote-original", status="connected"))
    tenant.commit()

    @contextmanager
    def tenant_session(route):
        with tenant_factory() as db:
            yield db

    monkeypatch.setattr(sessions, "tenant_session", tenant_session)
    monkeypatch.setattr(access, "cache_get_json", lambda *a: None)
    monkeypatch.setattr(access, "cache_set_json", lambda *a, **k: None)
    center.info["factory"] = center_factory
    tenant.info["factory"] = tenant_factory
    try:
        yield center, tenant
    finally:
        center.close()
        tenant.close()


def _issue(center):
    authorization.issue_link(center, channel="tiktok", payload=_link().context)
    return center.query(AuthorizationLink).order_by(AuthorizationLink.id.desc()).first()


def test_rotating_link_rejects_state_from_old_link(authorization_db, nonce_store):
    center, _ = authorization_db
    original = _issue(center)
    state = authorization.create_oauth_state(original)
    _issue(center)
    payload = authorization.consume_state(state, "tiktok")
    with pytest.raises(AppError):
        authorization.validate_link(center, payload["authorization_link_id"])


def test_disabled_member_cannot_complete_previously_issued_authorization(authorization_db):
    center, _ = authorization_db
    link = _issue(center)
    center.get(TenantMembership, 100).status = "disabled"
    center.commit()
    with pytest.raises(AppError):
        authorization.validate_link(center, link.id)


def test_connected_store_cannot_silently_change_remote_identity(authorization_db):
    center, _ = authorization_db
    link = _issue(center)
    with pytest.raises(AppError) as error:
        authorization.stage_result(center, {**link.context, "authorization_link_id": link.id}, {
            "shop": {"id": "different-shop"}, "token": {"access_token": "offline"},
        })
    assert error.value.status_code == 409
    center.refresh(link)
    assert link.result_cipher is None and link.status == "active"


def test_revoked_link_cannot_stage_credentials(authorization_db):
    center, _ = authorization_db
    link = _issue(center)
    link.status = "revoked"
    center.commit()
    with pytest.raises(AppError):
        authorization.stage_result(center, {**link.context, "authorization_link_id": link.id}, {
            "shop": {"id": "remote-original"}, "token": {"access_token": "offline"},
        })
    center.refresh(link)
    assert link.status == "revoked" and link.result_cipher is None
    assert center.query(ChannelAccountRegistry).count() == 0


def test_tenant_commit_failure_retains_encrypted_result_and_retry_recovers(authorization_db, monkeypatch):
    from app.db import session as sessions

    center, tenant = authorization_db
    link = _issue(center)
    payload = {**link.context, "authorization_link_id": link.id}
    center_factory = center.info["factory"]
    tenant_factory = tenant.info["factory"]
    failing = [True]

    @contextmanager
    def center_session():
        with center_factory() as db:
            yield db

    @contextmanager
    def tenant_session(route):
        with tenant_factory() as db:
            commit = db.commit

            def fail_first_commit():
                if failing[0]:
                    failing[0] = False
                    raise RuntimeError("simulated tenant commit interruption")
                commit()

            monkeypatch.setattr(db, "commit", fail_first_commit)
            yield db

    monkeypatch.setattr(sessions, "center_session", center_session)
    monkeypatch.setattr(sessions, "tenant_session", tenant_session)
    result = {"shop": {"id": "remote-original", "cipher": "offline-cipher", "name": "Remote"},
              "token": {"access_token": "offline-access", "refresh_token": "offline-refresh", "expire_in": 3600}}
    with pytest.raises(AppError):
        authorization.persist_result(payload, result)
    center.expire_all()
    tenant.expire_all()
    retained = center.get(AuthorizationLink, link.id)
    assert retained.status == "pending_save"
    assert "offline-access" not in retained.result_cipher
    assert authorization.saved_result(retained)["token"]["access_token"] == "offline-access"
    assert tenant.query(StoreCredential).count() == 0
    assert center.query(ChannelAccountRegistry).one().status == "reserved"

    # 不重新兑换授权码，直接使用上次已经保存的加密结果。
    output = authorization.persist_result(payload)
    center.expire_all()
    tenant.expire_all()
    assert output["store_id"] == 9
    assert tenant.query(Store).count() == 1 and tenant.query(StoreCredential).count() == 1
    restored = center.get(AuthorizationLink, link.id)
    assert restored.status == "active" and restored.result_cipher is None
    assert center.query(ChannelAccountRegistry).one().status == "bound"
