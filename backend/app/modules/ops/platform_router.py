"""平台管理员：服务器与实例运维接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import CenterDb, PlatformAdmin
from app.core.http import PageParams, paged, success
from app.modules.ops import service
from app.modules.ops.schemas import AlertAckIn, InstanceCreate, InstanceUpdate, MaintenanceWindowCreate, OperationCreate, ReleaseCreate

router = APIRouter(prefix="/platform", tags=["platform-ops"])


@router.get("/instances", summary="租户实例列表")
def list_instances(
    center: CenterDb,
    _: PlatformAdmin,
    pagination: PageParams = Depends(),
    q: str = Query(default="", max_length=100),
    status: str = Query(default="", max_length=32),
) -> dict:
    data = service.list_instances(
        center, page=pagination.page, page_size=pagination.page_size, q=q, status=status
    )
    return paged(data["items"], data["total"], data["page"], data["page_size"])


@router.post("/instances", summary="登记租户实例")
def create_instance(center: CenterDb, admin: PlatformAdmin, payload: InstanceCreate) -> dict:
    return success(service.create_instance(center, payload, actor_user_id=admin.user_id))


@router.get("/instances/{instance_id}", summary="租户实例详情")
def get_instance(center: CenterDb, _: PlatformAdmin, instance_id: int) -> dict:
    instance = service.get_instance(center, instance_id)
    from app.modules.identity.models_center import Tenant

    tenant = center.get(Tenant, instance.tenant_id)
    return success(service.instance_payload(center, instance, tenant.name if tenant else ""))


@router.patch("/instances/{instance_id}", summary="更新租户实例")
def update_instance(
    center: CenterDb,
    admin: PlatformAdmin,
    instance_id: int,
    payload: InstanceUpdate,
) -> dict:
    return success(service.update_instance(center, instance_id, payload, actor_user_id=admin.user_id))


@router.get("/instances/{instance_id}/nodes", summary="实例节点列表")
def list_nodes(center: CenterDb, _: PlatformAdmin, instance_id: int) -> dict:
    return success(service.list_nodes(center, instance_id))


@router.post("/instances/{instance_id}/nodes", summary="生成服务器接入令牌")
def create_node_enrollment(
    center: CenterDb, admin: PlatformAdmin, instance_id: int
) -> dict:
    return success(service.create_node_enrollment(center, instance_id, actor_user_id=admin.user_id))


@router.post("/nodes/{node_id}/probe", summary="立即探测节点公网访问")
def probe_node(center: CenterDb, _: PlatformAdmin, node_id: int) -> dict:
    return success(service.probe_node(center, node_id))


@router.get("/nodes/{node_id}/ssh", summary="读取服务器 SSH 配置")
def get_node_ssh(center: CenterDb, _: PlatformAdmin, node_id: int) -> dict:
    return success(service.get_ssh_config(center, node_id))


@router.put("/nodes/{node_id}/ssh", summary="保存服务器 SSH 账号")
def save_node_ssh(
    center: CenterDb, admin: PlatformAdmin, node_id: int, payload: NodeSshConfigIn
) -> dict:
    return success(service.save_ssh_config(center, node_id, payload, actor_user_id=admin.user_id))


@router.post("/nodes/{node_id}/ssh/exec", summary="通过 SSH 执行命令")
def exec_node_ssh(
    center: CenterDb, _: PlatformAdmin, node_id: int, payload: NodeSshExecIn
) -> dict:
    return success(service.ssh_execute(center, node_id, payload.command))


@router.get("/nodes/{node_id}/metrics", summary="节点指标趋势")
def node_metrics(
    center: CenterDb,
    _: PlatformAdmin,
    node_id: int,
    minutes: int = Query(default=60, ge=1, le=43200),
) -> dict:
    return success(service.list_metrics(center, node_id, minutes=minutes))


@router.get("/nodes/{node_id}/aggregates", summary="节点指标聚合趋势")
def node_aggregates(
    center: CenterDb,
    _: PlatformAdmin,
    node_id: int,
    resolution: str = Query(default="5m", pattern="^(5m|1h)$"),
    hours: int = Query(default=24, ge=1, le=720),
) -> dict:
    return success(service.list_metric_aggregates(center, node_id, resolution=resolution, hours=hours))


@router.get("/nodes/{node_id}/checks", summary="节点公网探测记录")
def node_checks(
    center: CenterDb,
    _: PlatformAdmin,
    node_id: int,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    return success(service.list_checks(center, node_id, limit=limit))


@router.get("/releases", summary="发布版本列表")
def releases(center: CenterDb, _: PlatformAdmin) -> dict:
    return success(service.list_releases(center))


@router.post("/releases", summary="登记发布版本")
def create_release(center: CenterDb, admin: PlatformAdmin, payload: ReleaseCreate) -> dict:
    return success(service.create_release(center, payload, actor_user_id=admin.user_id))


@router.get("/operations", summary="运维任务列表")
def operations(center: CenterDb, _: PlatformAdmin, pagination: PageParams = Depends(), instance_id: str = Query(default="", max_length=32)) -> dict:
    data = service.list_operations(center, instance_id=int(instance_id) if instance_id.isdigit() else None, page=pagination.page, page_size=pagination.page_size)
    return paged(data["items"], data["total"], data["page"], data["page_size"])


@router.post("/instances/{instance_id}/operations", summary="创建运维任务")
def create_operation(center: CenterDb, admin: PlatformAdmin, instance_id: int, payload: OperationCreate) -> dict:
    return success(service.create_operation(center, instance_id, payload, actor_user_id=admin.user_id))


@router.get("/operations/{job_id}", summary="运维任务详情")
def operation_detail(center: CenterDb, _: PlatformAdmin, job_id: int) -> dict:
    return success(service.get_operation(center, job_id))


@router.post("/operations/{job_id}/retry", summary="重试运维任务")
def retry_operation(center: CenterDb, admin: PlatformAdmin, job_id: int) -> dict:
    return success(service.retry_operation(center, job_id, actor_user_id=admin.user_id))


@router.get("/backups", summary="备份记录列表")
def backups(center: CenterDb, _: PlatformAdmin, pagination: PageParams = Depends(), instance_id: str = Query(default="", max_length=32)) -> dict:
    data = service.list_backups(center, instance_id=int(instance_id) if instance_id.isdigit() else None, page=pagination.page, page_size=pagination.page_size)
    return paged(data["items"], data["total"], data["page"], data["page_size"])


@router.get("/instances/{instance_id}/maintenance-windows", summary="实例维护窗口")
def maintenance_windows(center: CenterDb, _: PlatformAdmin, instance_id: int) -> dict:
    return success(service.list_maintenance_windows(center, instance_id))


@router.post("/instances/{instance_id}/maintenance-windows", summary="创建维护窗口")
def create_maintenance_window(center: CenterDb, admin: PlatformAdmin, instance_id: int, payload: MaintenanceWindowCreate) -> dict:
    return success(service.create_maintenance_window(center, instance_id, payload, actor_user_id=admin.user_id))


@router.delete("/maintenance-windows/{window_id}", summary="删除维护窗口")
def delete_maintenance_window(center: CenterDb, _: PlatformAdmin, window_id: int) -> dict:
    return success(service.delete_maintenance_window(center, window_id))


@router.get("/alerts", summary="告警列表")
def list_alerts(
    center: CenterDb,
    _: PlatformAdmin,
    pagination: PageParams = Depends(),
    status: str = Query(default="", max_length=16),
    node_id: str = Query(default="", max_length=32),
) -> dict:
    node_id_value = int(node_id) if node_id.isdigit() else None
    data = service.list_alerts(
        center,
        status=status,
        node_id=node_id_value,
        page=pagination.page,
        page_size=pagination.page_size,
    )
    return paged(data["items"], data["total"], data["page"], data["page_size"])


@router.post("/alerts/{alert_id}/ack", summary="确认告警")
def acknowledge_alert(
    center: CenterDb, admin: PlatformAdmin, alert_id: int, payload: AlertAckIn
) -> dict:
    return success(
        service.acknowledge_alert(
            center, alert_id, note=payload.note, actor_user_id=admin.user_id
        )
    )
