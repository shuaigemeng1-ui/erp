"""API 路由汇总。

模块路由分工（写作用域互斥，勿跨模块修改）：

* identity：``/auth``、``/me``、``/me/tenants`` 与租户内成员 / 角色路由
* platform：``/platform``
* catalog：``/tenants/{tenant_id}/products``
* inventory：``/tenants/{tenant_id}/inventory``
* channels：``/tenants/{tenant_id}/stores``、``/tenants/{tenant_id}/listings``、``/integrations/shopee``
* sales：``/tenants/{tenant_id}/orders``
* tasks：``/tenants/{tenant_id}/sync-runs``、``/tenants/{tenant_id}/tasks``
* audit：``/tenants/{tenant_id}/audit-logs``
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.mode_guard import http_mode_guard, http_tenant_binding_guard
from app.modules.audit.router import router as audit_router
from app.modules.catalog.router import router as catalog_router
from app.modules.channels.router import (
    integration_router,
    router as channels_router,
    tiktok_integration_router,
    tiktok_legacy_router,
)
from app.modules.identity.router import router as identity_router
from app.modules.identity.router import tenant_router as identity_tenant_router
from app.modules.inventory.router import router as inventory_router
from app.modules.platform.router import router as platform_router
from app.modules.sales.router import router as sales_router
from app.modules.tasks.router import router as tasks_router

from app.modules.channels.authorization import router as authorization_router
from app.modules.storage.router import router as storage_router
from app.modules.email.router import router as email_router
from app.modules.ops.agent_router import router as agent_router
from app.modules.ops.platform_router import router as ops_platform_router

# HTTP 路由统一挂部署形态守卫 + 固定实例租户绑定守卫：
# * 中心模式两者均放行，行为不变；
# * tenant 模式拒绝 /platform、/agent（同时由 ModeBoundaryMiddleware 在 ASGI 层兜底）；
# * tenant 模式请求中出现的 tenant_id 必须等于 settings.tenant_id，否则 403。
guarded_http_router = APIRouter(
    dependencies=[Depends(http_mode_guard), Depends(http_tenant_binding_guard)]
)
guarded_http_router.include_router(identity_router)
guarded_http_router.include_router(identity_tenant_router)
guarded_http_router.include_router(platform_router)
guarded_http_router.include_router(agent_router)
guarded_http_router.include_router(storage_router)
guarded_http_router.include_router(email_router)
guarded_http_router.include_router(catalog_router)
guarded_http_router.include_router(inventory_router)
guarded_http_router.include_router(channels_router)
guarded_http_router.include_router(sales_router)
guarded_http_router.include_router(tasks_router)
guarded_http_router.include_router(audit_router)
guarded_http_router.include_router(integration_router)
guarded_http_router.include_router(authorization_router)
guarded_http_router.include_router(tiktok_legacy_router)
guarded_http_router.include_router(tiktok_integration_router)

api_router = APIRouter()
api_router.include_router(guarded_http_router)
# 运维平台路由同时含 HTTP 与 WebSocket（平台 SSH 终端）：
# HTTP 侧由 ModeBoundaryMiddleware 与 main 中间件拦截；WS 侧由中间件按 path 直接 4404 关闭，
# 因此不能把它挂到只接受 HTTP 依赖的 guarded_http_router 上。
api_router.include_router(ops_platform_router)

__all__ = ["api_router"]
