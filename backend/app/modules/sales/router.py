"""订单路由（租户库）。

* ``GET  /tenants/{tenant_id}/orders``                 订单列表（按数据范围过滤）
* ``GET  /tenants/{tenant_id}/orders/{order_id}``     订单详情
* ``POST /tenants/{tenant_id}/orders/sync``           手动触发拉单（202 + task_id）

实现负责人：backend-sales。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/tenants/{tenant_id}/orders", tags=["orders"])
