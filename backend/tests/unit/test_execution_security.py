"""会话升级与批量任务中途撤权回归；只使用内存数据库和假 Redis。"""
from contextlib import contextmanager
import json

import pytest

from app.core import redis as sessions
from app.core.errors import AppError
from app.modules.access.models_tenant import TenantSettings
from app.modules.channels import service
from app.modules.identity.models_center import TenantMembership
from tests.unit.test_authorization_security import authorization_db  # noqa: F401


def test_old_session_rejected_and_new_session_accepted(monkeypatch):
    class Cache:
        values = {}
        def get(self, key): return self.values.get(key)
        def delete(self, key): self.values.pop(key, None)
        def setex(self, key, ttl, value): self.values[key] = value
        def sadd(self, *args): pass
        def expire(self, *args): pass
    cache = Cache()
    monkeypatch.setattr(sessions, "cache_redis", lambda: cache)
    cache.values["vs:sess:old"] = json.dumps({"user_id": 10, "scope": "tenant"})
    assert sessions.read_session("old") is None
    session_id, _, _ = sessions.create_session(10)
    assert sessions.read_session(session_id)["user_id"] == 10
    cache.values["vs:sess:bad"] = "not-json"
    assert sessions.read_session("bad") is None


def test_push_rechecks_actor_and_master_switch_between_skus(authorization_db, monkeypatch):
    from app.db import session as db_sessions
    center, tenant = authorization_db
    @contextmanager
    def center_session():
        with center.info["factory"]() as db:
            yield db
    monkeypatch.setattr(db_sessions, "center_session", center_session)
    tenant.add(TenantSettings(tenant_id=1, stock_push_enabled=True))
    tenant.commit()
    service._check_push_access(1, 9, 10)
    center.get(TenantMembership, 100).status = "disabled"
    center.commit()
    with pytest.raises(AppError):
        service._check_push_access(1, 9, 10)
    # 自动化不依赖手动操作者，但仍必须遵守总开关。
    settings = tenant.query(TenantSettings).filter_by(tenant_id=1).one()
    settings.stock_push_enabled = False
    tenant.commit()
    with pytest.raises(AppError):
        service._check_push_access(1, 9, None)


def test_store_scoped_role_cannot_edit_shared_catalog_or_read_global_cost():
    from app.core.access import EffectivePermissions, ScopeGrant
    from app.modules.catalog.service import _require_master_scope, can_read_cost
    scoped = EffectivePermissions(grants=(ScopeGrant(role_binding_id=1, role_id=1,
        scope_type="stores", scope_ids=frozenset({1}),
        permissions=frozenset({"catalog.product.update", "catalog.cost.read"})),))
    with pytest.raises(AppError):
        _require_master_scope(scoped, "catalog.product.update")
    assert can_read_cost(scoped) is False
