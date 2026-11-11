"""库存路由（租户库）。

* ``GET  /tenants/{tenant_id}/inventory``            库存余额与流水
* ``POST /tenants/{tenant_id}/inventory/adjustments`` 人工调整（禁止负库存）
* ``GET  /tenants/{tenant_id}/inventory/stock-places``

实现负责人：backend-catalog。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/tenants/{tenant_id}/inventory", tags=["inventory"])
