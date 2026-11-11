"""审计路由（租户库）。

* ``GET /tenants/{tenant_id}/audit-logs``：租户审计日志（需 ``audit.read``）

审计日志由各模块 Service 与业务写入同事务写入（``audit.service.record``），
本路由只读；``tenant_id`` 来自路径并由租户上下文校验，不接受请求体指定。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import (
    AccessSnapshotDep,
    RequirePermission,
    TenantCtx,
    TenantDb,
    require_permission,
)
from app.core.http import PageParams, paged
from app.core.permissions import Permission
from app.modules.audit import service as audit_service

router = APIRouter(prefix="/tenants/{tenant_id}/audit-logs", tags=["audit"])


@router.get("", summary="租户审计日志")
def list_audit_logs(
    ctx: TenantCtx,
    snapshot: AccessSnapshotDep,
    db: TenantDb,
    pagination: PageParams = Depends(),
    action: str = Query(default="", max_length=128, description="按操作编码过滤"),
    _: RequirePermission = Depends(require_permission(Permission.AUDIT_READ.value)),
) -> dict:
    data = audit_service.list_logs(
        db,
        tenant_id=ctx.tenant_id,
        page=pagination.page,
        page_size=pagination.page_size,
        action=action,
    )
    return paged(data["items"], data["total"], data["page"], data["page_size"])


__all__ = ["router"]
