"""任务路由（租户库）。

* ``GET  /tenants/{tenant_id}/sync-runs``       同步任务记录
* ``GET  /tenants/{tenant_id}/sync-runs/{id}``
* ``GET  /tenants/{tenant_id}/sync-schedules``  调度配置
* ``POST /tenants/{tenant_id}/sync-schedules``  保存调度配置
* ``POST /tenants/{tenant_id}/sync-runs/{id}/retry`` 重试

实现负责人：backend-sales（tasks 模块）。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["tasks"])
