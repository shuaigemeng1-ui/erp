"""网关安全原语：实例令牌/state 摘要、随机令牌、渠道密钥加密。

* 实例令牌与 OAuth state 只存 HMAC-SHA256 摘要（带主密钥与用途域），数据库泄漏时
  无法反推原始令牌；
* 比较使用常量时间；
* 渠道 App Secret 用主密钥派生的 Fernet 密钥加密保存（cryptography，网关独占）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Final

from cryptography.fernet import Fernet, InvalidToken

from app.modules.gateway.config import (
    INSTANCE_TOKEN_PREFIX,
    STATE_HASH_CONTEXT,
    TOKEN_HASH_CONTEXT,
    GatewayConfig,
)

_NONCE_BYTES: Final[int] = 32


def new_token(prefix: str = "", nbytes: int = _NONCE_BYTES) -> str:
    raw = secrets.token_urlsafe(nbytes)
    return f"{prefix}{raw}" if prefix else raw


def new_instance_token() -> str:
    return new_token(INSTANCE_TOKEN_PREFIX, 32)


def new_oauth_state() -> str:
    return new_token("", 32)


def hash_secret(config: GatewayConfig, value: str, context: str) -> str:
    """带主密钥与用途域的 HMAC-SHA256 摘要。"""
    key = config.secret_key.encode("utf-8")
    message = f"{context}|{value}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def instance_token_hash(config: GatewayConfig, token: str) -> str:
    return hash_secret(config, token, TOKEN_HASH_CONTEXT)


def oauth_state_hash(config: GatewayConfig, state: str) -> str:
    return hash_secret(config, state, STATE_HASH_CONTEXT)


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest((left or "").encode("utf-8"), (right or "").encode("utf-8"))


def _fernet(config: GatewayConfig) -> Fernet:
    digest = hashlib.sha256(
        f"gateway-channel-secret|{config.secret_key}".encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(config: GatewayConfig, plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet(config).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(config: GatewayConfig, ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet(config).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


__all__ = [
    "constant_time_equals",
    "decrypt_secret",
    "encrypt_secret",
    "hash_secret",
    "instance_token_hash",
    "new_instance_token",
    "new_oauth_state",
    "new_token",
    "oauth_state_hash",
]
