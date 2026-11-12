"""任务 API 的授权与执行开关回归；仅使用 SQLite 内存数据库。"""
from datetime import datetime

import pytest

from app.core.errors import AppError, ErrorCode
from app.core.access import EffectivePermissions, ScopeGrant
from app.modules.tasks import service, scheduler, outbox
from app.modules.tasks.models_tenant import SyncRun, SyncSchedule
from app.modules.access.models_tenant import TenantSettings
from app.db.registry import tenant_metadata
from tests.unit.test_identity import _route_client, _seed_tenant, _sqlite_metadata
from app.modules.identity import service as identity


@pytest.fixture()
def environment(monkeypatch):
    client, center, db, state = _route_client(monkeypatch, user_id=11)
    _sqlite_metadata(tenant_metadata, ('sync_runs', 'sync_schedules', 'outbox_events')).create_all(db.get_bind())
    roles = _seed_tenant(center, db)
    identity._write_bindings(db, tenant_id=1, member_id=101, bindings=[
        identity.RoleBindingChoice(role_id=int(roles['readonly'].id), scopes=(
            identity.ScopeChoice(scope_type='stores', scope_ids=(1,)),
        )),
    ], assigned_by=10)
    db.add(TenantSettings(tenant_id=1, permission_version=1, order_sync_enabled=True, stock_push_enabled=False))
    db.add_all([
        SyncRun(id=1, tenant_id=1, store_id=1, job_type='order_sync', status='failed', triggered_by='manual', requested_by=11, error_message='visible'),
        SyncRun(id=2, tenant_id=1, store_id=2, job_type='order_sync', status='failed', triggered_by='manual', requested_by=10, error_message='hidden'),
        SyncRun(id=3, tenant_id=1, store_id=None, job_type='order_sync', status='failed', triggered_by='schedule', error_message='all stores'),
        SyncSchedule(id=1, tenant_id=1, store_id=1, job_type='order_sync', interval_minutes=30, enabled=True),
        SyncSchedule(id=2, tenant_id=1, store_id=2, job_type='order_sync', interval_minutes=30, enabled=True),
    ])
    db.commit()
    yield client, db, state
    client.close(); center.close(); db.close()


def test_task_read_does_not_allow_schedule_changes_or_retry(environment):
    client, db, _ = environment
    response=client.put('/api/v1/tenants/1/sync-schedules/1',json={'enabled':False})
    assert response.status_code == 403
    assert db.get(SyncSchedule,1).enabled is True
    assert client.post('/api/v1/tenants/1/sync-runs/1/retry').status_code == 403


def test_task_queries_filter_store_scope_and_reject_unregistered_uuid(environment):
    client, _, _ = environment
    response=client.get('/api/v1/tenants/1/sync-runs')
    assert response.status_code == 200
    assert response.json()['data']['total'] == 1
    for suffix in ('sync-runs/2','sync-runs/3','tasks/2','tasks/3','tasks/unregistered-uuid'):
        assert client.get('/api/v1/tenants/1/'+suffix).status_code == 404
    schedules=client.get('/api/v1/tenants/1/sync-schedules').json()['data']
    assert [row['id'] for row in schedules] == ['1']
    assert client.get('/api/v1/tenants/1/tasks/1').status_code == 200


def test_feature_switch_blocks_creation_and_scheduler(environment):
    _, db, _ = environment
    with pytest.raises(AppError) as exc:
        service.request_sync_run(db, tenant_id=1, job_type='stock_push',store_id=1)
    assert exc.value.code == ErrorCode.FEATURE_DISABLED
    settings=db.query(TenantSettings).filter_by(tenant_id=1).one()
    settings.order_sync_enabled=False
    db.commit()
    summary=scheduler.run_tenant_schedules(db,1,now=datetime.utcnow())
    assert summary['created'] == 0


def test_disabled_stock_push_outbox_stays_pending(environment, monkeypatch):
    _, db, _ = environment
    service.enqueue_outbox(db,tenant_id=1,event_type='stock.push.requested',aggregate_type='store',aggregate_id=1,payload={'tenant_id':1,'store_id':1,'sku_ids':[1]})
    db.commit()
    sent=[]
    monkeypatch.setattr(outbox,'_dispatch_one',lambda event:(sent.append(event.id) or True,'',None))
    result=outbox.dispatch_tenant_batch(db,1,limit=10)
    assert sent == []
    assert result['dispatched'] == 0


def test_scope_check_requires_explicit_all_for_tenant_wide_job():
    effective=EffectivePermissions(grants=(ScopeGrant(role_binding_id=1,role_id=1,scope_type='stores',scope_ids=frozenset({1}),permissions=frozenset({'order.sync'})),))
    service.ensure_job_access(effective,'order_sync',1)
    with pytest.raises(AppError):
        service.ensure_job_access(effective,'order_sync',None)


def test_pending_jobs_do_not_replace_manual_actor_with_automation(environment):
    _, db, _ = environment
    scheduled=service.request_sync_run(db,tenant_id=1,job_type='order_sync',store_id=1,triggered_by='schedule')
    first=service.request_sync_run(db,tenant_id=1,job_type='order_sync',store_id=1,requested_by=10)
    second=service.request_sync_run(db,tenant_id=1,job_type='order_sync',store_id=1,requested_by=11)
    repeated=service.request_sync_run(db,tenant_id=1,job_type='order_sync',store_id=1,requested_by=11)
    assert len({scheduled,first,second}) == 3
    assert second == repeated
    with pytest.raises(AppError):
        service.request_sync_run(db,tenant_id=1,job_type='order_sync',store_id=1)


def test_listing_sync_registers_numeric_task_before_dispatch(environment,monkeypatch):
    from types import SimpleNamespace
    from app.modules.channels import router as channel_router
    _, db, state = environment
    # 以预置所有者身份调用平台商品同步。
    state['user_id']=10
    client=environment[0]
    monkeypatch.setattr(channel_router.service,'get_store_or_raise',lambda *args:SimpleNamespace(status='connected'))
    monkeypatch.setattr(channel_router,'_dispatch_task',lambda *args,**kwargs:pytest.fail('must use persisted outbox'))
    response=client.post('/api/v1/tenants/1/listings/sync',json={'store_id':'1'})
    assert response.status_code == 202
    task_id=response.json()['data']['task_id']
    assert task_id.isdigit()
    run=db.get(SyncRun,int(task_id))
    assert run.job_type == 'listing_sync'
    assert run.requested_by == 10
    assert client.get('/api/v1/tenants/1/tasks/'+task_id).status_code == 200


def test_sync_settings_require_management_and_invalidate_permissions(environment):
    client, db, state = environment
    assert client.get('/api/v1/tenants/1/sync-settings').status_code == 403
    assert client.put('/api/v1/tenants/1/sync-settings',json={'stock_push_enabled':True}).status_code == 403
    state['user_id']=10
    original=db.query(TenantSettings).filter_by(tenant_id=1).one().permission_version
    response=client.put('/api/v1/tenants/1/sync-settings',json={'stock_push_enabled':True,'order_sync_enabled':False})
    assert response.status_code == 200
    assert response.json()['data'] == {'order_sync_enabled':False,'stock_push_enabled':True}
    row=db.query(TenantSettings).filter_by(tenant_id=1).one()
    assert row.permission_version == original + 1
    assert service.job_enabled(db,1,'stock_push') is True
    assert service.job_enabled(db,1,'order_sync') is False


def test_disabled_master_switch_overrides_feature_display_flags(environment):
    from app.modules.access.models_tenant import FeatureSetting
    from app.modules.access.service import _load_feature_flags
    _, db, _ = environment
    row=db.query(TenantSettings).filter_by(tenant_id=1).one()
    row.order_sync_enabled=False
    row.stock_push_enabled=False
    db.add_all([
        FeatureSetting(tenant_id=1,feature_code='stock_push',enabled=True),
        FeatureSetting(tenant_id=1,feature_code='order_sync',enabled=True),
    ])
    db.commit()
    flags=_load_feature_flags(db,1,row)
    assert 'stock_push' not in flags
    assert 'order_sync' not in flags
