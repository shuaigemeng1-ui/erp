"""平台管理路由（中心库，仅平台管理员）。

* ``GET  /platform/tenants``：租户分页列表
* ``POST /platform/tenants``：创建租户并执行开通流水线（``202`` + 任务ID）
* ``GET  /platform/tenants/{id}``：租户详情（含开通步骤与数据库状态）
* ``POST /platform/tenants/{id}/retry-provision``：失败开通重试（``202``）
* ``POST /platform/tenants/{id}/suspend``：暂停租户（禁止租户业务访问与主动同步）
* ``POST /platform/tenants/{id}/activate``：重新启用（先核对平台状态）
* ``GET  /platform/provisioning-jobs/{id}``：开通任务状态与失败原因
* ``GET/POST /platform/plans``、``PATCH /platform/plans/{id}``：套餐与额度
* ``GET/PUT /platform/channel-apps/{channel}/{environment}``：平台渠道应用（密钥不回显）
* ``GET  /platform/audit-logs``：平台管理审计

首版开通流水线在请求内同步执行（无需额外 Worker 即可完成开通）；
``202`` 响应仍返回任务标识与开通步骤，前端可轮询租户详情查看进度。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse

from app.api.deps import CenterDb, PlatformAdmin
from app.core.errors import AppError, ErrorCode
from app.core.http import PageParams, accepted, paged, success
from app.db.session import tenant_session
from app.modules.identity import provisioning
from app.modules.identity import repository as repo
from app.modules.identity import service
from app.modules.identity.schemas import OwnerTransferRequest
from app.modules.platform import service as platform_service
from app.modules.platform.schemas import (
    ChannelAppPutRequest,
    PlanCreateRequest,
    PlanUpdateRequest,
    SuspendTenantRequest,
    TenantCreateRequest,
)

router = APIRouter(prefix="/platform", tags=["platform"])


@router.get("/tenants", summary="租户列表")
def list_tenants(
    center: CenterDb,
    _: PlatformAdmin,
    pagination: PageParams = Depends(),
    status: str | None = Query(default=None, description="租户状态过滤"),
    q: str = Query(default="", max_length=100, description="按编码或名称搜索"),
) -> dict:
    tenants, total = repo.list_tenants(
        center,
        status=status,
        keyword=q,
        offset=(pagination.page - 1) * pagination.page_size,
        limit=pagination.page_size,
    )
    items = [service.tenant_list_payload(center, tenant) for tenant in tenants]
    return paged(items, total, pagination.page, pagination.page_size)


@router.post("/tenants", summary="创建租户并投递开通任务", status_code=202)
def create_tenant(payload: TenantCreateRequest, center: CenterDb, admin: PlatformAdmin) -> JSONResponse:
    """中心库登记租户 + 创建开通任务行，投递 Celery 后立即返回 ``202``。

    响应中的 ``task_id``：异步模式为 Celery 任务 ID，降级同步模式为 ``provisioning_jobs.id``；
    ``provisioning_job_id`` 始终是首个开通步骤行，可用于 ``GET /platform/provisioning-jobs/{id}`` 轮询。
    """
    tenant, jobs = provisioning.create_tenant(
        center,
        code=payload.code,
        name=payload.name,
        owner_email=payload.owner_email,
        timezone=payload.timezone,
        plan_id=payload.plan_id,
        operator_user_id=admin.user_id,
    )
    tenant_id = int(tenant.id)
    platform_service.record_platform_audit(
        center,
        actor_user_id=admin.user_id,
        action="tenant.create",
        resource_type="tenant",
        resource_id=tenant_id,
        tenant_id=tenant_id,
        summary=f"创建租户：{tenant.name}",
        payload={"code": tenant.code, "owner_email": payload.owner_email},
    )
    center.commit()
    dispatch = provisioning.dispatch_provisioning(center, tenant_id, operator_user_id=admin.user_id)
    tenant = repo.get_tenant(center, tenant_id)
    data = {
        "task_id": dispatch["task_id"],
        "mode": dispatch["mode"],
        "provisioning_job_id": dispatch["provisioning_job_id"],
        "tenant": service.tenant_detail_payload(center, tenant) if tenant is not None else None,
        "provisioning": dispatch["provisioning"],
    }
    return accepted(data)


@router.get("/tenants/{tenant_id}", summary="租户详情")
def get_tenant(
    center: CenterDb,
    _: PlatformAdmin,
    tenant_id: int = Path(ge=1),
) -> dict:
    tenant = repo.get_tenant(center, tenant_id)
    if tenant is None:
        raise AppError(ErrorCode.TENANT_NOT_FOUND)
    data = service.tenant_detail_payload(center, tenant)
    data["subscription"] = platform_service.subscription_payload(center, tenant_id)
    return success(data)


@router.post("/tenants/{tenant_id}/retry-provision", summary="重试开通（投递任务）", status_code=202)
def retry_provision(center: CenterDb, admin: PlatformAdmin, tenant_id: int = Path(ge=1)) -> JSONResponse:
    """重试开通失败 / 中断的租户；投递 Celery 后立即返回 ``202``。"""
    dispatch = provisioning.dispatch_provisioning(
        center, tenant_id, operator_user_id=admin.user_id, retry=True
    )
    platform_service.record_platform_audit(
        center,
        actor_user_id=admin.user_id,
        action="tenant.retry_provision",
        resource_type="tenant",
        resource_id=tenant_id,
        tenant_id=tenant_id,
        summary="重试租户开通",
        payload={"mode": dispatch["mode"], "task_id": dispatch["task_id"]},
    )
    center.commit()
    tenant = repo.get_tenant(center, tenant_id)
    return accepted(
        {
            "task_id": dispatch["task_id"],
            "mode": dispatch["mode"],
            "provisioning_job_id": dispatch["provisioning_job_id"],
            "tenant": service.tenant_detail_payload(center, tenant) if tenant is not None else None,
            "provisioning": dispatch["provisioning"],
        }
    )


@router.post("/tenants/{tenant_id}/suspend", summary="暂停租户")
def suspend_tenant(
    payload: SuspendTenantRequest,
    center: CenterDb,
    admin: PlatformAdmin,
    tenant_id: int = Path(ge=1),
) -> dict:
    tenant = provisioning.suspend_tenant(
        center, tenant_id=tenant_id, reason=payload.reason, operator_user_id=admin.user_id
    )
    platform_service.record_platform_audit(
        center,
        actor_user_id=admin.user_id,
        action="tenant.suspend",
        resource_type="tenant",
        resource_id=tenant_id,
        tenant_id=tenant_id,
        summary=f"暂停租户：{payload.reason}",
        payload={"reason": payload.reason},
    )
    center.commit()
    return success(service.tenant_detail_payload(center, tenant))


@router.post("/tenants/{tenant_id}/activate", summary="重新启用租户")
def activate_tenant(center: CenterDb, admin: PlatformAdmin, tenant_id: int = Path(ge=1)) -> dict:
    tenant = provisioning.activate_tenant(center, tenant_id=tenant_id, operator_user_id=admin.user_id)
    platform_service.record_platform_audit(
        center,
        actor_user_id=admin.user_id,
        action="tenant.activate",
        resource_type="tenant",
        resource_id=tenant_id,
        tenant_id=tenant_id,
        summary="重新启用租户",
    )
    center.commit()
    return success(service.tenant_detail_payload(center, tenant))


@router.post("/tenants/{tenant_id}/owner-transfer", summary="平台转移租户所有者")
def transfer_tenant_owner(
    payload: OwnerTransferRequest,
    center: CenterDb,
    admin: PlatformAdmin,
    tenant_id: int = Path(ge=1),
) -> dict:
    """平台管理员强制转移所有者（租户负责人离职 / 失联时的兜底）。"""
    route = service.load_tenant_db_route(center, tenant_id)
    if route is None:
        raise AppError(ErrorCode.TENANT_DATABASE_UNAVAILABLE, "租户数据库不可用，无法转移")
    with tenant_session(route) as db:
        data = service.transfer_owner(
            center,
            db,
            tenant_id=tenant_id,
            target_member_id=payload.target_member_id,
            actor_user_id=admin.user_id,
            actor_is_platform_admin=True,
        )
    platform_service.record_platform_audit(
        center,
        actor_user_id=admin.user_id,
        action="tenant.owner_transfer",
        resource_type="tenant",
        resource_id=tenant_id,
        tenant_id=tenant_id,
        summary=f"平台转移租户所有者给成员 {payload.target_member_id}",
        payload=data,
    )
    center.commit()
    return success(data)

@router.get("/provisioning-jobs/{job_id}", summary="开通任务状态")
def get_provisioning_job(center: CenterDb, _: PlatformAdmin, job_id: int = Path(ge=1)) -> dict:
    job = repo.get_provisioning_job(center, job_id)
    if job is None:
        raise AppError(ErrorCode.TASK_NOT_FOUND)
    from app.modules.identity.constants import PROVISIONING_STEP_LABELS

    return success(
        {
            "id": str(job.id),
            "tenant_id": str(job.tenant_id),
            "step": job.step,
            "label": PROVISIONING_STEP_LABELS.get(job.step, job.step),
            "status": job.status,
            "attempts": job.attempts,
            "error_message": job.error_message,
            "started_at": service.iso_utc(job.started_at),
            "finished_at": service.iso_utc(job.finished_at),
        }
    )


# --------------------------------------------------------------------------- 套餐
@router.get("/plans", summary="套餐列表")
def list_plans(center: CenterDb, _: PlatformAdmin) -> dict:
    return success(platform_service.list_plans(center))


@router.post("/plans", summary="创建套餐")
def create_plan(payload: PlanCreateRequest, center: CenterDb, admin: PlatformAdmin) -> dict:
    data = platform_service.create_plan(
        center,
        code=payload.code,
        name=payload.name,
        description=payload.description,
        features=[feature.model_dump() for feature in payload.features],
        actor_user_id=admin.user_id,
    )
    return success(data)


@router.patch("/plans/{plan_id}", summary="调整套餐")
def update_plan(
    payload: PlanUpdateRequest,
    center: CenterDb,
    admin: PlatformAdmin,
    plan_id: int = Path(ge=1),
) -> dict:
    data = platform_service.update_plan(
        center,
        plan_id=plan_id,
        name=payload.name,
        description=payload.description,
        status=payload.status,
        features=[feature.model_dump() for feature in payload.features] if payload.features is not None else None,
        actor_user_id=admin.user_id,
    )
    return success(data)


# --------------------------------------------------------------------------- 渠道应用
@router.get("/channel-apps/{channel}/{environment}", summary="读取平台渠道应用")
def get_channel_app(
    center: CenterDb,
    _: PlatformAdmin,
    channel: str = Path(min_length=1, max_length=32),
    environment: str = Path(min_length=1, max_length=16),
) -> dict:
    return success(platform_service.get_channel_app(center, channel, environment))


@router.put("/channel-apps/{channel}/{environment}", summary="保存平台渠道应用")
def put_channel_app(
    payload: ChannelAppPutRequest,
    center: CenterDb,
    admin: PlatformAdmin,
    channel: str = Path(min_length=1, max_length=32),
    environment: str = Path(min_length=1, max_length=16),
) -> dict:
    data = platform_service.save_channel_app(
        center,
        channel=channel,
        environment=environment,
        partner_id=payload.partner_id,
        partner_key=payload.partner_key,
        partner_secret=payload.partner_secret,
        redirect_url=payload.redirect_url,
        actor_user_id=admin.user_id,
    )
    return success(data)


# --------------------------------------------------------------------------- 平台审计
@router.get("/audit-logs", summary="平台管理审计")
def list_platform_audit_logs(
    center: CenterDb,
    _: PlatformAdmin,
    pagination: PageParams = Depends(),
    action: str = Query(default="", max_length=128),
) -> dict:
    data = platform_service.list_platform_audit_logs(
        center, page=pagination.page, page_size=pagination.page_size, action=action
    )
    return paged(data["items"], data["total"], data["page"], data["page_size"])


__all__ = ["router"]
