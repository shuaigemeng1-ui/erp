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

from fastapi import APIRouter

from app.modules.audit.router import router as audit_router
from app.modules.catalog.router import router as catalog_router
from app.modules.channels.router import integration_router, router as channels_router
from app.modules.identity.router import router as identity_router
from app.modules.identity.router import tenant_router as identity_tenant_router
from app.modules.inventory.router import router as inventory_router
from app.modules.platform.router import router as platform_router
from app.modules.sales.router import router as sales_router
from app.modules.tasks.router import router as tasks_router

api_router = APIRouter()
api_router.include_router(identity_router)
api_router.include_router(identity_tenant_router)
api_router.include_router(platform_router)
api_router.include_router(catalog_router)
api_router.include_router(inventory_router)
api_router.include_router(channels_router)
api_router.include_router(sales_router)
api_router.include_router(tasks_router)
api_router.include_router(audit_router)
api_router.include_router(integration_router)

__all__ = ["api_router"]
