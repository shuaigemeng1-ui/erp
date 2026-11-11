"""审计路由（租户库）。

* ``GET /tenants/{tenant_id}/audit-logs`` 租户审计日志（按数据范围过滤）

实现负责人：backend-identity（audit 服务与路由）。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/tenants/{tenant_id}/audit-logs", tags=["audit"])
