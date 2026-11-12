"""平台存储账号管理、默认选择与上传入口，凭据加密且永不回显。"""
from __future__ import annotations
import re
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import AppError, ConflictError, ErrorCode
from app.core.security import decrypt_secret, encrypt_secret
from app.db.base import utcnow
from app.modules.platform.service import record_platform_audit
from app.modules.storage.models_center import StorageProfile, SystemSettings
from app.modules.storage.schemas import StorageProfileInput, StorageProfileUpdate

PROVIDERS = (
    {"code": "oss", "name": "阿里云 OSS"},
    {"code": "cos", "name": "腾讯云 COS"},
    {"code": "obs", "name": "华为云 OBS"},
    {"code": "s3", "name": "AWS S3"},
    {"code": "s3_compatible", "name": "S3 兼容服务"},
)
PROFILE_FIELDS = ("name", "provider", "endpoint", "bucket", "region", "public_base_url", "key_prefix", "addressing_style", "enabled")


def profile_payload(row):
    return {"id": str(row.id), **{key: getattr(row, key) for key in PROFILE_FIELDS},
            "version": row.version, "has_access_key": bool(row.access_key_id_cipher),
            "has_secret_key": bool(row.secret_access_key_cipher)}


def get_settings(center):
    settings = center.get(SystemSettings, 1)
    rows = center.execute(select(StorageProfile).order_by(StorageProfile.id)).scalars()
    return {"default_storage_profile_id": str(settings.default_storage_profile_id) if settings and settings.default_storage_profile_id else None,
            "version": settings.version if settings else 1, "profiles": [profile_payload(row) for row in rows],
            "providers": list(PROVIDERS)}


def _lock_settings(center):
    row = center.execute(select(SystemSettings).where(SystemSettings.id == 1)
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None:
        row = SystemSettings(id=1, version=1)
        center.add(row)
        center.flush()
    return row


def _profile(center, profile_id, *, lock=False):
    stmt = select(StorageProfile).where(StorageProfile.id == profile_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = center.execute(stmt).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "对象存储账号不存在")
    return row


def _secret(value):
    return value.get_secret_value() if value is not None else ""


def _region(payload):
    if payload.region:
        return payload.region
    hostname = urlsplit(payload.endpoint).hostname or ""
    patterns = {
        "oss": r"(?:s3\.)?oss-([^.]+)\.aliyuncs\.com$",
        "cos": r"cos\.([^.]+)\.myqcloud\.com$",
        "obs": r"obs\.([^.]+)\.myhuaweicloud\.com$",
        "s3": r"s3[.-]([^.]+)\.amazonaws\.com(?:\.cn)?$",
    }
    match = re.fullmatch(patterns.get(payload.provider, r"(?!)"), hostname)
    return match[1] if match else "us-east-1"


def save_profile(center, payload: StorageProfileInput, *, actor_user_id: int, profile_id: int | None = None):
    """按固定锁顺序修改配置；更新必须有版本，空凭据仅保留同厂商旧值。"""
    try:
        settings = _lock_settings(center)
        row = _profile(center, profile_id, lock=True) if profile_id is not None else None
        if row is not None and (not isinstance(payload, StorageProfileUpdate) or payload.version != row.version):
            raise ConflictError("存储配置已被其他管理员修改，请刷新后保存")
        access_key = _secret(payload.access_key_id)
        secret_key = _secret(payload.secret_access_key)
        provider_changed = row is not None and row.provider != payload.provider
        if (row is None or provider_changed) and (not access_key or not secret_key):
            raise AppError(ErrorCode.VALIDATION_ERROR, "新增账号或更换云厂商时必须填写完整访问密钥")
        if row is not None and settings.default_storage_profile_id == row.id and not payload.enabled:
            raise ConflictError("当前默认存储账号不能停用，请先切换默认账号")
        values = {name: getattr(payload, name) for name in PROFILE_FIELDS}
        values["region"] = _region(payload)
        changes = list(PROFILE_FIELDS) if row is None else [name for name in PROFILE_FIELDS if getattr(row, name) != values[name]]
        if access_key:
            changes.append("access_key_updated")
        if secret_key:
            changes.append("secret_key_updated")
        if row is None:
            row = StorageProfile(**values, version=1,
                access_key_id_cipher=encrypt_secret(access_key), secret_access_key_cipher=encrypt_secret(secret_key))
            center.add(row)
            center.flush()
            action = "system.storage.create"
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
            action = "system.storage.update"
        if changes:
            record_platform_audit(center, actor_user_id=actor_user_id, action=action,
                resource_type="storage_profile", resource_id=row.id,
                summary=f"保存对象存储账号：{row.name}", payload={"fields": changes, "provider": row.provider})
        center.commit()
        return profile_payload(row)
    except IntegrityError as exc:
        center.rollback()
        raise ConflictError("存储账号名称已存在或配置发生并发变更，请刷新重试") from exc
    except Exception:
        center.rollback()
        raise


def set_default(center, profile_id: int, *, version: int, actor_user_id: int):
    try:
        settings = _lock_settings(center)
        if settings.version != version:
            raise ConflictError("系统配置已变更，请刷新后重新选择默认账号")
        profile = _profile(center, profile_id, lock=True)
        if not profile.enabled:
            raise AppError(ErrorCode.VALIDATION_ERROR, "不能将已停用账号设为默认存储")
        if not profile.access_key_id_cipher or not profile.secret_access_key_cipher:
            raise AppError(ErrorCode.VALIDATION_ERROR, "对象存储访问密钥尚未配置完整")
        if settings.default_storage_profile_id != profile.id:
            old_id = settings.default_storage_profile_id
            settings.default_storage_profile_id = profile.id
            settings.version += 1
            settings.updated_at = utcnow()
            record_platform_audit(center, actor_user_id=actor_user_id, action="system.storage.default",
                resource_type="system_settings", resource_id=1, summary="切换默认对象存储账号",
                payload={"old_profile_id": str(old_id) if old_id else None, "profile_id": str(profile.id)})
        center.commit()
        return get_settings(center)
    except IntegrityError as exc:
        center.rollback()
        raise ConflictError("系统配置发生并发变更，请刷新重试") from exc
    except Exception:
        center.rollback()
        raise


def _config(row):
    from app.modules.storage.providers import StorageConfig
    try:
        access_key = decrypt_secret(row.access_key_id_cipher)
        secret_key = decrypt_secret(row.secret_access_key_cipher)
    except Exception:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "对象存储密钥无法读取，请联系平台管理员重新配置") from None
    return StorageConfig(profile_id=row.id, provider=row.provider, endpoint=row.endpoint,
        bucket=row.bucket, region=row.region, access_key_id=access_key, secret_access_key=secret_key,
        public_base_url=row.public_base_url, key_prefix=row.key_prefix, addressing_style=row.addressing_style)


def get_default_config(center):
    """只在上传当次读取当前配置，不缓存明文凭据。"""
    settings = center.execute(select(SystemSettings).where(SystemSettings.id == 1)
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if settings is None or not settings.default_storage_profile_id:
        raise AppError(ErrorCode.VALIDATION_ERROR, "请先由平台管理员配置并启用默认对象存储")
    row = _profile(center, settings.default_storage_profile_id)
    if not row.enabled:
        raise AppError(ErrorCode.VALIDATION_ERROR, "默认对象存储账号已停用，请联系平台管理员")
    return _config(row)


def upload_default(center, tenant_id: int, *, object_key: str, data: bytes, content_type: str):
    if tenant_id <= 0 or not object_key.startswith(f"catalog/{tenant_id}/"):
        raise AppError(ErrorCode.VALIDATION_ERROR, "对象存储键与租户不一致")
    from app.modules.storage.providers import create_client
    config = get_default_config(center)
    try:
        with create_client(config) as client:
            return client.put_object(object_key, data, content_type)
    except Exception:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "图片上传至对象存储失败，请检查存储账号、桶权限和网络后重试") from None


def test_profile(center, profile_id: int):
    from app.modules.storage.providers import create_client
    row = _profile(center, profile_id)
    config = _config(row)
    try:
        with create_client(config) as client:
            client.head_bucket()
    except Exception:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "对象存储连接失败，请检查服务地址、存储桶和访问密钥") from None
    return {"ok": True, "message": "存储桶连接检查通过"}
