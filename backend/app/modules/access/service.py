"""权限计算服务契约。

``app/api/deps.py`` 依赖 ``AccessSnapshot`` 与 ``load_access_snapshot``，签名固定。

实现要求：

* 读取租户设置中的 ``permission_version``。
* 汇总成员的有效角色绑定（含数据范围）与权限，构造 ``EffectivePermissions``。
* 缓存键包含 tenant、member、permission_version；版本递增后旧缓存自然失效。
* 角色分配检查授予权限，防止通过编辑角色 / 调整范围自行提权。

实现负责人：backend-identity。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.access import EffectivePermissions


@dataclass(frozen=True, slots=True)
class AccessSnapshot:
    """一次请求内的权限快照。"""

    permission_version: int
    effective: EffectivePermissions
    feature_flags: frozenset[str] = field(default_factory=frozenset)
    loaded_at: datetime | None = None


def load_access_snapshot(db: Session, tenant_id: int, member_id: int) -> AccessSnapshot:
    """加载成员的有效授权快照（租户库 Session）。"""
    raise NotImplementedError("access.service.load_access_snapshot 尚未实现")
