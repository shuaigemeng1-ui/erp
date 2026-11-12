"""平台管理服务：平台审计、套餐额度、渠道应用（复用 channels 模块实现）。

平台管理全部写 ``platform_audit_logs``（中心库），与业务写入同一事务。
渠道应用（Shopee Partner Key 等）的表与加密逻辑由 channels 模块负责，
本模块只做平台接口与审计，通过函数内延迟 import 调用，模块未就绪时抛 ``AppError``。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import get_request_id
from app.core.errors import AppError, ErrorCode
from app.modules.identity.constants import PLATFORM_AUDIT_ACTIONS
from app.modules.platform.models_center import (
    Plan,
    PlanFeature,
    PlatformAuditLog,
    Subscription,
)

logger = logging.getLogger(__name__)

VALID_ENVIRONMENTS = ("production", "test")


# --------------------------------------------------------------------------- 平台审计
def record_platform_audit(
    center: Session,
    *,
    actor_user_id: int | None,
    action: str,
    resource_type: str,
    resource_id: int | None = None,
    tenant_id: int | None = None,
    summary: str = "",
    payload: dict[str, Any] | None = None,
    request_id: str = "",
    ip: str = "",
) -> int:
    """写平台管理审计（不提交事务，由调用方提交）。"""
    row = PlatformAuditLog(
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        tenant_id=tenant_id,
        summary=(summary or PLATFORM_AUDIT_ACTIONS.get(action, action))[:255],
        payload=payload,
        ip=ip or "",
        request_id=request_id or _safe_request_id(),
    )
    center.add(row)
    center.flush()
    return int(row.id)


def _safe_request_id() -> str:
    try:
        return get_request_id()
    except Exception:  # noqa: BLE001 - 无请求上下文（CLI / Worker）时留空
        return ""


def platform_audit_payload(row: PlatformAuditLog) -> dict[str, Any]:
    from app.modules.identity.service import iso_utc

    return {
        "id": str(row.id),
        "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
        "action": row.action,
        "action_label": PLATFORM_AUDIT_ACTIONS.get(row.action, ""),
        "resource_type": row.resource_type,
        "resource_id": str(row.resource_id) if row.resource_id else None,
        "tenant_id": str(row.tenant_id) if row.tenant_id else None,
        "summary": row.summary,
        "payload": row.payload,
        "ip": row.ip,
        "request_id": row.request_id,
        "created_at": iso_utc(row.created_at),
    }


def list_platform_audit_logs(
    center: Session, *, page: int, page_size: int, action: str = ""
) -> dict[str, Any]:
    stmt = select(PlatformAuditLog)
    if action:
        stmt = stmt.where(PlatformAuditLog.action == action)
    total = int(center.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    rows = (
        center.execute(
            stmt.order_by(PlatformAuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return {
        "items": [platform_audit_payload(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- 套餐
def plan_payload(plan: Plan, features: list[PlanFeature] | None = None) -> dict[str, Any]:
    from app.modules.identity.service import iso_utc

    return {
        "id": str(plan.id),
        "code": plan.code,
        "name": plan.name,
        "status": plan.status,
        "description": plan.description,
        "is_default": bool(plan.is_default),
        "features": [
            {
                "feature_code": feature.feature_code,
                "limit_value": feature.limit_value,
            }
            for feature in (features or [])
        ],
        "created_at": iso_utc(plan.created_at),
        "updated_at": iso_utc(plan.updated_at),
    }


def list_plans(center: Session) -> list[dict[str, Any]]:
    plans = list(center.execute(select(Plan).order_by(Plan.id)).scalars())
    features_by_plan: dict[int, list[PlanFeature]] = {}
    if plans:
        for feature in center.execute(
            select(PlanFeature)
            .where(PlanFeature.plan_id.in_([int(plan.id) for plan in plans]))
            .order_by(PlanFeature.feature_code)
        ).scalars():
            features_by_plan.setdefault(int(feature.plan_id), []).append(feature)
    return [plan_payload(plan, features_by_plan.get(int(plan.id))) for plan in plans]


def get_plan(center: Session, plan_id: int) -> Plan | None:
    return center.get(Plan, plan_id)


def create_plan(
    center: Session,
    *,
    code: str,
    name: str,
    description: str = "",
    features: list[dict[str, Any]] | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    """创建套餐（不实现线上购买，仅平台管理员配置）。"""
    normalized_code = (code or "").strip().lower()
    if not normalized_code:
        raise AppError(ErrorCode.VALIDATION_ERROR, "套餐编码不能为空")
    if center.execute(select(Plan).where(Plan.code == normalized_code)).scalar_one_or_none() is not None:
        raise AppError(ErrorCode.CONFLICT, "套餐编码已存在")
    plan = Plan(
        code=normalized_code,
        name=(name or normalized_code)[:64],
        status="active",
        description=(description or "")[:255],
        is_default=False,
    )
    center.add(plan)
    center.flush()
    _replace_plan_features(center, int(plan.id), features or [])
    record_platform_audit(
        center,
        actor_user_id=actor_user_id,
        action="plan.create",
        resource_type="plan",
        resource_id=int(plan.id),
        summary=f"创建套餐：{plan.name}",
        payload={"code": plan.code},
    )
    center.commit()
    return plan_payload(plan, _plan_features(center, int(plan.id)))


def update_plan(
    center: Session,
    *,
    plan_id: int,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    features: list[dict[str, Any]] | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    """调整套餐：额度降低不删除已有数据，只影响后续新增。"""
    plan = center.get(Plan, plan_id)
    if plan is None:
        raise AppError(ErrorCode.NOT_FOUND, "套餐不存在")
    if status is not None and status not in {"active", "disabled"}:
        raise AppError(ErrorCode.VALIDATION_ERROR, "套餐状态只能是 active 或 disabled")
    if name is not None:
        if not name.strip():
            raise AppError(ErrorCode.VALIDATION_ERROR, "套餐名称不能为空")
        plan.name = name.strip()[:64]
    if description is not None:
        plan.description = description[:255]
    if status is not None:
        plan.status = status
    center.add(plan)
    if features is not None:
        _replace_plan_features(center, plan_id, features)
    record_platform_audit(
        center,
        actor_user_id=actor_user_id,
        action="plan.update",
        resource_type="plan",
        resource_id=plan_id,
        summary=f"调整套餐：{plan.name}",
        payload={"features": features},
    )
    center.commit()
    return plan_payload(plan, _plan_features(center, plan_id))


def _plan_features(center: Session, plan_id: int) -> list[PlanFeature]:
    return list(
        center.execute(
            select(PlanFeature)
            .where(PlanFeature.plan_id == plan_id)
            .order_by(PlanFeature.feature_code)
        ).scalars()
    )


def _replace_plan_features(center: Session, plan_id: int, features: list[dict[str, Any]]) -> None:
    existing = {feature.feature_code: feature for feature in _plan_features(center, plan_id)}
    seen: set[str] = set()
    for item in features:
        feature_code = str(item.get("feature_code") or "").strip()
        if not feature_code:
            raise AppError(ErrorCode.VALIDATION_ERROR, "额度编码不能为空")
        if feature_code in seen:
            raise AppError(ErrorCode.VALIDATION_ERROR, f"额度编码重复：{feature_code}")
        seen.add(feature_code)
        limit_value = item.get("limit_value")
        if limit_value is not None and int(limit_value) < 0:
            raise AppError(ErrorCode.VALIDATION_ERROR, "额度不能为负数")
        row = existing.get(feature_code)
        if row is None:
            center.add(
                PlanFeature(
                    plan_id=plan_id,
                    feature_code=feature_code,
                    limit_value=int(limit_value) if limit_value is not None else None,
                )
            )
            continue
        row.limit_value = int(limit_value) if limit_value is not None else None
        center.add(row)
    for feature_code, row in existing.items():
        if feature_code not in seen:
            center.delete(row)
    center.flush()


# --------------------------------------------------------------------------- 订阅
def subscription_payload(center: Session, tenant_id: int) -> dict[str, Any] | None:
    from app.modules.identity.service import iso_utc

    row = center.execute(
        select(Subscription)
        .where(Subscription.tenant_id == tenant_id)
        .order_by(Subscription.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    plan = center.get(Plan, int(row.plan_id))
    return {
        "id": str(row.id),
        "plan_id": str(row.plan_id),
        "plan_code": plan.code if plan else "",
        "plan_name": plan.name if plan else "",
        "status": row.status,
        "starts_at": iso_utc(row.starts_at),
        "ends_at": iso_utc(row.ends_at),
    }


# --------------------------------------------------------------------------- 渠道应用
def _channel_service():
    """延迟导入 channels 模块的渠道应用接口（模块未就绪时转 AppError）。"""
    try:
        from app.modules.channels import service as channel_service
    except Exception as exc:  # noqa: BLE001 - 模块未实现 / 导入失败
        logger.warning("channels.service 尚不可用: %s", exc)
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "渠道模块尚未就绪") from exc
    for name in ("load_channel_app", "channel_app_public_view", "save_channel_app"):
        if not hasattr(channel_service, name):
            raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "渠道应用接口尚未实现")
    return channel_service


def _normalize_environment(environment: str) -> str:
    value = (environment or "").strip().lower()
    if value not in VALID_ENVIRONMENTS:
        raise AppError(ErrorCode.VALIDATION_ERROR, "环境只能是 production 或 test")
    return value


def get_channel_app(center: Session, channel: str, environment: str) -> dict[str, Any]:
    """读取平台渠道应用配置（密钥不回显）。"""
    channel_service = _channel_service()
    config = channel_service.load_channel_app(center, (channel or "").strip(), _normalize_environment(environment))
    if config is None:
        raise AppError(ErrorCode.NOT_FOUND, "渠道应用未配置")
    return channel_service.channel_app_public_view(config)


def save_channel_app(
    center: Session,
    *,
    channel: str,
    environment: str,
    partner_id: int | None,
    partner_key: str | None,
    partner_secret: str | None,
    redirect_url: str | None,
    actor_user_id: int | None,
    app_key: str | None = None,
    app_secret: str | None = None,
) -> dict[str, Any]:
    """保存平台渠道应用凭据：内部使用 core.security 加密，密钥禁止回显。"""
    from app.modules.channels.constants import CHANNEL_TIKTOK

    channel_service = _channel_service()
    normalized_channel = (channel or "").strip().lower()
    normalized_environment = _normalize_environment(environment)
    if normalized_channel != CHANNEL_TIKTOK and partner_id is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "partner_id 不能为空")
    config = channel_service.save_channel_app(
        center,
        normalized_channel,
        normalized_environment,
        partner_id=partner_id,
        partner_key=partner_key,
        partner_secret=partner_secret,
        app_key=app_key,
        app_secret=app_secret,
        redirect_url=redirect_url,
        actor_id=actor_user_id,
    )
    record_platform_audit(
        center,
        actor_user_id=actor_user_id,
        action="channel_app.update",
        resource_type="channel_app",
        resource_id=None,
        summary=f"保存渠道应用：{normalized_channel}/{normalized_environment}",
        payload={
            "channel": normalized_channel,
            "environment": normalized_environment,
            "partner_id": partner_id,
            "app_key": config.app_key or None,
            "redirect_url": redirect_url,
            "partner_key_changed": partner_key is not None,
            "partner_secret_changed": partner_secret is not None,
            "app_secret_changed": app_secret is not None,
        },
    )
    center.commit()
    return channel_service.channel_app_public_view(config)
