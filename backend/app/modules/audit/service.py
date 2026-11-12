"""审计服务契约（跨模块调用）。

租户内敏感操作（成员 / 角色 / 范围 / 店铺 / 库存调整 / 任务重试）必须写审计：

* 与业务写操作同一租户库事务；
* 记录租户、操作人、动作、对象、结果摘要、request_id；
* 禁止在审计 payload 中保存明文密码 / Token。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import get_request_id
from app.modules.audit.models_tenant import AuditLog


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
    product_id: int | None = None,
    sku_id: int | None = None,
    actor_name_snapshot: str | None = None,
) -> int:
    """写一条租户审计日志，返回日志 ID。

    本函数只 ``flush``，不提交事务：由调用方（Service）与业务写入一起提交。
    """
    row = AuditLog(
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        summary=(summary or "")[:255],
        payload=payload,
        ip=ip or "",
        request_id=request_id or _safe_request_id(),
        product_id=product_id, sku_id=sku_id, actor_name_snapshot=actor_name_snapshot,
    )
    db.add(row)
    db.flush()
    return int(row.id)


def _safe_request_id() -> str:
    try:
        return get_request_id()
    except Exception:  # noqa: BLE001 - 无请求上下文（Worker / CLI）时使用空标识
        return ""


def audit_payload(row: AuditLog, *, include_cost: bool = False) -> dict[str, Any]:
    """审计日志响应结构（ID 一律字符串、时间 UTC ISO）。"""
    from app.modules.identity.service import iso_utc

    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "actor_id": str(row.actor_id) if row.actor_id else None,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": str(row.resource_id) if row.resource_id else None,
        "summary": row.summary,
        "payload": visible_payload(row.payload, include_cost=include_cost),
        "ip": row.ip,
        "request_id": row.request_id,
        "created_at": iso_utc(row.created_at),
    }


def list_logs(
    db: Session, *, tenant_id: int, page: int, page_size: int, action: str = "", include_cost: bool = False,
) -> dict[str, Any]:
    """租户审计分页查询（列表与总数使用同一过滤条件）。"""
    stmt = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    rows = (
        db.execute(
            stmt.order_by(AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
        )
        .scalars()
        .all()
    )
    return {
        "items": [audit_payload(row, include_cost=include_cost) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def visible_payload(payload: dict | None, *, include_cost: bool) -> dict | None:
    """商品成本差异在所有审计出口应用同一权限过滤。"""
    if not payload or include_cost:
        return payload
    result = dict(payload)
    if "changes" in result:
        result["changes"] = [change for change in result["changes"] if not change.get("sensitive")]
    return result
