"""店铺与渠道路由（租户库 + Shopee 回调）。

* ``GET/POST /tenants/{tenant_id}/stores``             店铺列表 / 发起授权
* ``DELETE    /tenants/{tenant_id}/stores/{store_id}`` 断开授权
* ``GET       /tenants/{tenant_id}/listings``          平台商品读取与映射
* ``POST      /tenants/{tenant_id}/listings/{id}/link`` 建立 / 解除 SKU 映射
* ``GET/POST  /integrations/shopee/oauth/callback``    Shopee 授权回调（签名校验，不走用户会话）
* ``POST      /integrations/shopee/webhook``           Shopee 推送（验签 + 防重）

实现负责人：backend-channels。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["channels"])
integration_router = APIRouter(prefix="/integrations/shopee", tags=["shopee"])
