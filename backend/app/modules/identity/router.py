"""身份与租户路由。

实现范围（P1/P2）：

* ``GET  /auth/csrf``            获取匿名 CSRF 令牌（登录前）
* ``POST /auth/login``           登录（同源 Cookie 会话）
* ``POST /auth/logout``          退出
* ``POST /auth/password``        修改密码（撤销其它会话）
* ``GET  /auth/invitations/{token}``  邀请信息预览
* ``POST /auth/invitations/accept``   激活邀请并设置密码
* ``GET  /me``                   当前账号
* ``GET  /me/tenants``           当前账号可访问租户
* 租户路由：``GET /tenants/{tenant_id}/bootstrap``、members、roles
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["identity"])
tenant_router = APIRouter(tags=["tenant"])
