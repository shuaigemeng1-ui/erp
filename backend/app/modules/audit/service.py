"""审计服务契约（跨模块调用）。

租户内敏感操作（成员 / 角色 / 范围 / 店铺 / 库存调整 / 任务重试）必须写审计：

* 与业务写操作同一租户库事务；
* 记录租户、操作人、动作、对象、结果摘要、request_id；
* 禁止在审计 payload 中保存明文密码 / Token。

实现负责人：backend-identity（audit 模块）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


def record(
    db: Session,
    *,
    tenant_id: int,
    action: str,
    resource_type: str,
    resource_id: int | None = None,
    actor_id: int | None = None,
    summary: str = "",
    payload: dict[str, Any] | None = None,
    request_id: str = "",
    ip: str = "",
) -> int:
    """写一条租户审计日志，返回日志 ID。"""
    raise NotImplementedError("audit.service.record 尚未实现")
