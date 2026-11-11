"""身份服务契约。

``app/api/deps.py`` 依赖以下四个函数，签名固定，不得修改：
调用方只传中心库 Session 与用户 / 租户标识，返回 ``None`` 表示不可见。

实现负责人：backend-identity。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedUser, TenantAccessInfo
from app.core.context import TenantDbRoute


def load_authenticated_user(center: Session, user_id: int) -> AuthenticatedUser | None:
    """加载登录用户（含是否平台管理员）。用户不存在返回 ``None``。"""
    raise NotImplementedError("identity.service.load_authenticated_user 尚未实现")


def load_tenant_access(
    center: Session, tenant_id: int, user_id: int
) -> TenantAccessInfo | None:
    """校验用户与租户成员关系，返回租户访问信息；不存在或已移除返回 ``None``。"""
    raise NotImplementedError("identity.service.load_tenant_access 尚未实现")


def load_tenant_db_route(center: Session, tenant_id: int) -> TenantDbRoute | None:
    """从中心库受控映射读取租户库连接信息（含加密凭据解密）。"""
    raise NotImplementedError("identity.service.load_tenant_db_route 尚未实现")


def is_platform_admin(center: Session, user_id: int) -> bool:
    """是否拥有平台管理员角色。"""
    raise NotImplementedError("identity.service.is_platform_admin 尚未实现")
