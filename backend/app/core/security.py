"""密码、令牌、CSRF、对称加密。

安全约定：

* 密码使用 bcrypt 单向哈希，绝不记录明文。
* 邀请 / 重置密码令牌只存 SHA-256 摘要，原文只在生成时返回一次。
* Shopee 应用 Secret、店铺 access/refresh token 使用 AES 对称加密后入库，
  数据库文件与备份泄漏时不直接暴露凭据。
* 所有比较使用常量时间比较。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Final

import bcrypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

_TOKEN_BYTES: Final[int] = 32
_NONCE_BYTES: Final[int] = 12


# --------------------------------------------------------------------------- 密码
def hash_password(password: str) -> str:
    """bcrypt 哈希；bcrypt 只接受 72 字节以内的输入，先做 SHA-256 预摘要。"""
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return bcrypt.hashpw(digest, bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        digest = hashlib.sha256(password.encode("utf-8")).digest()
        return bcrypt.checkpw(digest, password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


PASSWORD_MIN_LENGTH: Final[int] = 10


def validate_password_strength(password: str) -> None:
    """基础密码策略：长度 + 字符类别。失败抛 ``AppError``。"""
    from app.core.errors import AppError, ErrorCode

    if len(password) < PASSWORD_MIN_LENGTH:
        raise AppError(ErrorCode.AUTH_PASSWORD_TOO_WEAK, f"密码至少需要 {PASSWORD_MIN_LENGTH} 位")
    classes = sum(
        1
        for predicate in (
            any(char.islower() for char in password),
            any(char.isupper() for char in password),
            any(char.isdigit() for char in password),
            any(not char.isalnum() for char in password),
        )
        if predicate
    )
    if classes < 3:
        raise AppError(ErrorCode.AUTH_PASSWORD_TOO_WEAK, "密码需包含大小写字母、数字、符号中的至少三类")


# --------------------------------------------------------------------------- 令牌
def random_token(nbytes: int = _TOKEN_BYTES) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def peppered_hash(value: str) -> str:
    return hmac.new(settings.secret_key_bytes, value.encode("utf-8"), hashlib.sha256).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def new_session_id() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_after(seconds: int) -> datetime:
    return utc_now() + timedelta(seconds=seconds)


# --------------------------------------------------------------------------- 对称加密
def _aes_key() -> bytes:
    return settings.derived_encryption_key


def encrypt_secret(plaintext: str) -> str:
    """返回 ``enc:v1:<base64(nonce+ciphertext)>``；空串原样返回。"""
    if plaintext is None:
        return ""
    if plaintext == "":
        return ""
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(_aes_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    return f"{settings.token_encryption_prefix}{payload}"


def decrypt_secret(stored: str) -> str:
    """解密 ``encrypt_secret`` 的产物；兼容历史明文（无前缀时原样返回）。"""
    if not stored:
        return ""
    prefix = settings.token_encryption_prefix
    if not stored.startswith(prefix):
        return stored
    payload = stored[len(prefix) :]
    raw = base64.urlsafe_b64decode(payload.encode("ascii"))
    nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    return AESGCM(_aes_key()).decrypt(nonce, ciphertext, None).decode("utf-8")


def mask_secret(value: str, keep: int = 4) -> str:
    """日志 / 展示用脱敏：只保留末尾若干字符。"""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]
