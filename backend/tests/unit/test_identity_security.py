"""邀请身份和角色范围回归：使用内存数据库，不连接外部服务。"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.core.access import EffectivePermissions, ScopeGrant
from app.core.errors import AppError, ErrorCode
from app.core.permissions import ALL_PERMISSIONS
from app.core.security import hash_password, verify_password
from app.modules.access import service as access_service
from app.modules.identity import repository, router, service
from app.modules.identity.models_center import TenantMembership, User
from tests.unit.test_identity import _build_sqlite_sessions, _route_client, _seed_tenant


@pytest.fixture
def identity_data(monkeypatch):
    cf, tf, ce, te = _build_sqlite_sessions()
    center, db = cf(), tf()
    roles = _seed_tenant(center, db)
    owner = EffectivePermissions(grants=(ScopeGrant(
        role_binding_id=1, role_id=int(roles['owner'].id), scope_type='all',
        permissions=ALL_PERMISSIONS,
    ),))
    monkeypatch.setattr(access_service, 'cache_get_json', lambda key: None)
    monkeypatch.setattr(access_service, 'cache_set_json', lambda *a, **k: None)

    @contextmanager
    def tenant_session(route):
        yield db

    monkeypatch.setattr(service, 'tenant_session', tenant_session)
    monkeypatch.setattr(service, 'list_user_tenants', lambda center, user_id: [])
    yield center, db, roles, owner
    center.close()
    db.close()
    ce.dispose()
    te.dispose()


def invite(center, db, roles, owner, *, existing=True):
    user = User(id=33, email='invitee@example.test', display_name='Invitee',
                password_hash=hash_password('Original-Password-881!') if existing else None,
                status='active', locale='zh-CN')
    center.add(user)
    center.commit()
    payload = service.create_member_invitation(
        center, db, tenant_id=1, email=user.email, actor_id=10, actor_effective=owner,
        bindings=[service.RoleBindingChoice(role_id=int(roles['readonly'].id),
                  scopes=(service.ScopeChoice(scope_type='all'),))],
    )
    return user, payload['invitation_token']


def test_existing_invitee_cannot_login_via_inviter_token(identity_data, monkeypatch):
    center, db, roles, owner = identity_data
    user, token = invite(center, db, roles, owner)
    client, _, _, state = _route_client(monkeypatch, sessions=(center, db))
    issued = []
    monkeypatch.setattr(router, 'create_session', lambda *a, **k: issued.append(a))
    # 邀请人持有返回的明文令牌，但不能取得受邀人的账号身份。
    response = client.post('/api/v1/auth/invitations/accept', json={
        'token': token, 'password': 'Unrelated-Password-991!',
    })
    assert response.status_code == 403
    assert issued == []
    assert verify_password('Original-Password-881!', user.password_hash)
    assert service.preview_invitation(center, token)['requires_login'] is True
    state['user_id'] = 0
    response = client.post('/api/v1/auth/invitations/accept', json={'token': token})
    assert response.status_code == 401
    assert issued == []


def test_existing_invitee_logged_in_can_join_without_password_reset(identity_data, monkeypatch):
    center, db, roles, owner = identity_data
    user, token = invite(center, db, roles, owner)
    client, _, _, state = _route_client(monkeypatch, user_id=33, sessions=(center, db))
    issued = []

    def session_stub(user_id, **kwargs):
        issued.append(user_id)
        return 'test-session', {}, 'test-csrf'

    monkeypatch.setattr(router, 'create_session', session_stub)
    monkeypatch.setattr(router, 'delete_session', lambda sid: None)
    response = client.post('/api/v1/auth/invitations/accept', json={'token': token})
    assert response.status_code == 200, response.text
    assert issued == [33]
    assert verify_password('Original-Password-881!', user.password_hash)
    membership = repository.get_membership(center, 1, 33)
    assert membership.status == 'active'
    with pytest.raises(AppError) as repeated:
        service.accept_invitation(center, token=token, authenticated_user_id=33)
    assert repeated.value.code is ErrorCode.AUTH_TOKEN_USED


def test_disabled_global_account_is_not_reactivated_by_invitation(identity_data):
    center, db, roles, owner = identity_data
    user, token = invite(center, db, roles, owner)
    user.status = 'disabled'
    center.commit()
    with pytest.raises(AppError) as rejected:
        service.accept_invitation(center, token=token, authenticated_user_id=33)
    assert rejected.value.code is ErrorCode.AUTH_ACCOUNT_DISABLED
    center.refresh(user)
    assert user.status == 'disabled'
    assert service.preview_invitation(center, token)['status'] == 'pending'


def test_new_account_activation_still_sets_password(identity_data):
    center, db, roles, owner = identity_data
    user, token = invite(center, db, roles, owner, existing=False)
    assert service.preview_invitation(center, token)['requires_login'] is False
    accepted, _ = service.accept_invitation(center, token=token, password='New-Password-771!')
    assert accepted.id == 33
    assert verify_password('New-Password-771!', accepted.password_hash)
    assert repository.get_membership(center, 1, 33).status == 'active'


def test_tenant_binding_failure_leaves_invitation_retryable(identity_data, monkeypatch):
    center, db, roles, owner = identity_data
    user, token = invite(center, db, roles, owner, existing=False)
    original = service._write_bindings

    def fail(*args, **kwargs):
        raise RuntimeError('simulated tenant write failure')

    monkeypatch.setattr(service, '_write_bindings', fail)
    with pytest.raises(RuntimeError):
        service.accept_invitation(center, token=token, password='New-Password-771!')
    center.refresh(user)
    assert user.password_hash is None
    assert service.preview_invitation(center, token)['status'] == 'pending'
    assert repository.get_membership(center, 1, 33).status == 'invited'
    monkeypatch.setattr(service, '_write_bindings', original)
    service.accept_invitation(center, token=token, password='New-Password-771!')
    assert repository.get_membership(center, 1, 33).status == 'active'


def test_role_edit_cannot_expand_scoped_permission_through_global_binding(identity_data):
    center, db, roles, owner = identity_data
    limited = service.create_role(
        db, tenant_id=1, code='limited', name='Limited', permissions=['role.manage', 'stock.push'],
        copy_from_role_id=None, actor_id=10, actor_effective=owner,
    )
    service._write_bindings(db, tenant_id=1, member_id=101, assigned_by=10, bindings=[
        service.RoleBindingChoice(role_id=int(roles['readonly'].id), scopes=(service.ScopeChoice('all'),)),
        service.RoleBindingChoice(role_id=int(limited['id']), scopes=(service.ScopeChoice('stores', (1,)),)),
    ])
    db.commit()
    actor = access_service.load_access_snapshot(db, 1, 101).effective
    assert actor.covers('stock.push', store_id=1)
    assert not actor.covers('stock.push', store_id=999)
    codes = repository.role_permission_map(db, 1, [int(roles['readonly'].id)])[int(roles['readonly'].id)]
    with pytest.raises(AppError) as rejected:
        service.update_role(db, tenant_id=1, role_id=int(roles['readonly'].id),
                            permissions=codes + ['stock.push'], actor_id=11, actor_effective=actor)
    assert rejected.value.code is ErrorCode.ROLE_ASSIGN_FORBIDDEN
    assert not access_service.load_access_snapshot(db, 1, 101).effective.covers('stock.push', store_id=999)


def test_owner_permissions_cannot_be_cleared(identity_data):
    _, db, roles, owner = identity_data
    with pytest.raises(AppError) as rejected:
        service.update_role(db, tenant_id=1, role_id=int(roles['owner'].id), permissions=[],
                            actor_id=10, actor_effective=owner)
    assert rejected.value.code is ErrorCode.CONFLICT
