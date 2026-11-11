"""任务服务契约（跨模块调用）。

* ``create_sync_run``：异步操作返回 202 时先落任务记录，再投递 Outbox。
* ``enqueue_outbox``：与业务数据同一事务写入 outbox_events。
* 手动任务执行前复核成员权限（``app.api.deps.ensure_permission``）。

实现负责人：backend-sales（tasks 模块）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session


def enqueue_outbox(
    db: Session,
    *,
    tenant_id: int,
    event_type: str,
    aggregate_type: str,
    aggregate_id: int | None,
    payload: dict[str, object] | None = None,
    available_delay_seconds: int = 0,
) -> int:
    """在租户库事务内写入待投递事件，返回事件 ID。"""
    raise NotImplementedError("tasks.service.enqueue_outbox 尚未实现")
