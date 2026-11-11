"""商品路由（租户库）。

* ``GET/POST /tenants/{tenant_id}/products``
* ``GET/PATCH /tenants/{tenant_id}/products/{product_id}``
* SKU 与图片、成本字段白名单过滤

实现负责人：backend-catalog。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/tenants/{tenant_id}/products", tags=["catalog"])
