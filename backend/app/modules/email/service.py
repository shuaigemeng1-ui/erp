"""平台邮件服务配置与发送入口。

邮件账号的 AccessKey/Secret 只加密落在中心库，接口永不回显明文。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import AppError, ConflictError, ErrorCode
from app.core.security import decrypt_secret, encrypt_secret
from app.db.base import utcnow
from app.modules.email.models_center import EmailProfile
from app.modules.email.providers import (
    EmailConfig,
    EmailMessage,
    EmailProviderError,
    send_email,
)
from app.modules.email.schemas import EmailProfileInput, EmailProfileUpdate
from app.modules.platform.service import record_platform_audit

PROVIDERS = (
    {
        "code": "aliyun_directmail",
        "name": "阿里云 DirectMail",
        "default_region": "cn-hangzhou",
        "region_label": "地域 ID，默认 cn-hangzhou",
    },
    {
        "code": "tencent_ses",
        "name": "腾讯云 SES",
        "default_region": "ap-guangzhou",
        "region_label": "地域，默认 ap-guangzhou",
    },
)
PROVIDER_CODES = frozenset(item["code"] for item in PROVIDERS)
PROFILE_FIELDS = (
    "name",
    "provider",
    "region",
    "from_address",
    "from_name",
    "enabled",
)


def _secret(value) -> str:
    return value.get_secret_value() if value is not None else ""


def _default_region(provider: str) -> str:
    for item in PROVIDERS:
        if item["code"] == provider:
            return str(item["default_region"])
    return ""


def profile_payload(row: EmailProfile) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "provider": row.provider,
        "region": row.region,
        "from_address": row.from_address,
        "from_name": row.from_name,
        "enabled": row.enabled,
        "is_default": row.is_default,
        "version": row.version,
        "has_access_key": bool(row.access_key_id_cipher),
        "has_secret_key": bool(row.secret_access_key_cipher),
    }


def list_profiles(center) -> dict:
    rows = center.execute(select(EmailProfile).order_by(EmailProfile.id)).scalars().all()
    return {
        "profiles": [profile_payload(row) for row in rows],
        "providers": list(PROVIDERS),
    }


def get_profile(center, profile_id: int, *, lock: bool = False) -> EmailProfile:
    stmt = select(EmailProfile).where(EmailProfile.id == int(profile_id))
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = center.execute(stmt).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "邮件配置不存在")
    return row


def _clear_defaults(center, *, except_id: int | None = None) -> None:
    rows = center.execute(select(EmailProfile)).scalars().all()
    for row in rows:
        if except_id is not None and row.id == except_id:
            continue
        if row.is_default:
            row.is_default = False


def save_profile(
    center,
    payload: EmailProfileInput,
    *,
    actor_user_id: int,
    profile_id: int | None = None,
) -> dict:
    try:
        row = get_profile(center, profile_id, lock=True) if profile_id is not None else None
        existing_count = int(center.execute(select(func.count()).select_from(EmailProfile)).scalar_one())
        if row is not None and (
            not isinstance(payload, EmailProfileUpdate) or payload.version != row.version
        ):
            raise ConflictError("邮件配置已被其他管理员修改，请刷新后保存")
        access_key = _secret(payload.access_key_id)
        secret_key = _secret(payload.secret_access_key)
        provider_changed = row is not None and row.provider != payload.provider
        if (row is None or provider_changed) and (not access_key or not secret_key):
            raise AppError(ErrorCode.VALIDATION_ERROR, "新增账号或更换服务商时必须填写完整密钥")
        if payload.provider not in PROVIDER_CODES:
            raise AppError(ErrorCode.VALIDATION_ERROR, "不支持的邮件服务商")
        if row is not None and row.is_default and not payload.enabled:
            raise ConflictError("默认邮件账号不能停用，请先切换默认账号")
        region = (payload.region or "").strip() or _default_region(payload.provider)
        values = {
            "name": payload.name,
            "provider": payload.provider,
            "region": region,
            "from_address": str(payload.from_address),
            "from_name": payload.from_name,
            "enabled": payload.enabled,
        }
        changes = (
            list(PROFILE_FIELDS)
            if row is None
            else [name for name in PROFILE_FIELDS if getattr(row, name) != values[name]]
        )
        if access_key:
            changes.append("access_key_updated")
        if secret_key:
            changes.append("secret_key_updated")
        if row is None:
            row = EmailProfile(
                **values,
                version=1,
                is_default=existing_count == 0,
                access_key_id_cipher=encrypt_secret(access_key),
                secret_access_key_cipher=encrypt_secret(secret_key),
            )
            center.add(row)
            center.flush()
            action = "system.email.create"
        else:
            for name in PROFILE_FIELDS:
                setattr(row, name, values[name])
            if access_key:
                row.access_key_id_cipher = encrypt_secret(access_key)
            if secret_key:
                row.secret_access_key_cipher = encrypt_secret(secret_key)
            if changes:
                row.version += 1
                row.updated_at = utcnow()
            action = "system.email.update"
        if changes:
            record_platform_audit(
                center,
                actor_user_id=actor_user_id,
                action=action,
                resource_type="email_profile",
                resource_id=row.id,
                summary=f"保存邮件账号：{row.name}",
                payload={"fields": changes, "provider": row.provider},
            )
        center.commit()
        return profile_payload(row)
    except IntegrityError as exc:
        center.rollback()
        raise ConflictError("邮件账号名称已存在或配置发生并发变更，请刷新后重试") from exc
    except Exception:
        center.rollback()
        raise


def set_default(center, profile_id: int, *, actor_user_id: int) -> dict:
    try:
        row = get_profile(center, profile_id, lock=True)
        if not row.enabled:
            raise AppError(ErrorCode.VALIDATION_ERROR, "已停用的邮件账号不能设为默认")
        if not row.access_key_id_cipher or not row.secret_access_key_cipher:
            raise AppError(ErrorCode.VALIDATION_ERROR, "邮件账号密钥尚未配置完整")
        if not row.is_default:
            _clear_defaults(center, except_id=row.id)
            row.is_default = True
            row.updated_at = utcnow()
            record_platform_audit(
                center,
                actor_user_id=actor_user_id,
                action="system.email.default",
                resource_type="email_profile",
                resource_id=row.id,
                summary=f"切换默认邮件账号：{row.name}",
            )
        center.commit()
        return profile_payload(row)
    except Exception:
        center.rollback()
        raise


def _config(row: EmailProfile) -> EmailConfig:
    try:
        access_key = decrypt_secret(row.access_key_id_cipher)
        secret_key = decrypt_secret(row.secret_access_key_cipher)
    except Exception:
        raise AppError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "邮件服务密钥无法读取，请平台管理员重新配置",
        ) from None
    return EmailConfig(
        provider=row.provider,
        region=row.region,
        from_address=row.from_address,
        from_name=row.from_name,
        access_key_id=access_key,
        secret_access_key=secret_key,
    )


def get_default_config(center) -> EmailConfig:
    row = center.execute(
        select(EmailProfile).where(EmailProfile.is_default.is_(True))
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "请先在系统配置中设置默认邮件账号")
    if not row.enabled:
        raise AppError(ErrorCode.VALIDATION_ERROR, "默认邮件账号已停用，请平台管理员处理")
    return _config(row)


def send_default(
    center,
    *,
    to: list[str],
    subject: str,
    text: str = "",
    html: str = "",
) -> dict:
    config = get_default_config(center)
    message = EmailMessage(to=to, subject=subject, text=text, html=html)
    try:
        result = send_email(config, message)
    except EmailProviderError as exc:
        raise AppError(
            ErrorCode.SERVICE_UNAVAILABLE,
            exc.message,
            details={"provider_code": exc.code},
        ) from None
    return {
        "ok": result.ok,
        "message": result.message,
        "provider": result.provider,
        "request_id": result.request_id,
    }


def test_profile(center, profile_id: int, *, to: str, subject: str = "ERP 邮件服务测试") -> dict:
    row = get_profile(center, profile_id)
    config = _config(row)
    message = EmailMessage(
        to=[to],
        subject=subject,
        text="这是一封来自 ERP 系统配置的测试邮件。收到说明邮件服务配置可用。",
        html=(
            "<p>这是一封来自 ERP 系统配置的测试邮件。</p>"
            "<p>收到说明邮件服务配置可用。</p>"
        ),
    )
    try:
        result = send_email(config, message)
    except EmailProviderError as exc:
        raise AppError(
            ErrorCode.SERVICE_UNAVAILABLE,
            exc.message,
            details={"provider_code": exc.code},
        ) from None
    return {
        "ok": result.ok,
        "message": result.message,
        "provider": result.provider,
        "request_id": result.request_id,
    }
