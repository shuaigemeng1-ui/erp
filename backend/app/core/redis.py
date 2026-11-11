"""Redis 缓存与登录会话存储。

* 缓存 Redis 与队列 Redis 分开配置（``cache_redis_url`` / ``queue_redis_url``）。
* 会话只存 Redis，Cookie 中只放随机会话 ID；会话可被退出、账号停用、密码变更撤销。
* 所有键都带业务前缀；涉及租户的键必须包含 ``tenant_id``。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from redis import Redis

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.security import new_csrf_token, new_session_id, utc_now

SESSION_KEY_PREFIX = "vs:sess:"
USER_SESSIONS_KEY_PREFIX = "vs:user:sess:"
CSRF_KEY_PREFIX = "vs:csrf:"
CACHE_KEY_PREFIX = "vs:cache:"


@lru_cache(maxsize=1)
def cache_redis() -> Redis:
    """缓存/会话 Redis 客户端（惰性创建，导入本模块不会连接）。"""
    return Redis.from_url(settings.cache_redis_url, decode_responses=True)


def _session_key(session_id: str) -> str:
    return f"{SESSION_KEY_PREFIX}{session_id}"


def _user_sessions_key(user_id: int) -> str:
    return f"{USER_SESSIONS_KEY_PREFIX}{user_id}"


def create_session(user_id: int) -> tuple[str, dict[str, Any], str]:
    """创建登录会话，返回 ``(session_id, payload, csrf_token)``。"""
    session_id = new_session_id()
    csrf_token = new_csrf_token()
    now_ts = int(utc_now().timestamp())
    payload: dict[str, Any] = {
        "user_id": user_id,
        "csrf": csrf_token,
        "created_at": now_ts,
        "last_seen": now_ts,
    }
    client = cache_redis()
    ttl = settings.session_ttl_seconds
    client.setex(_session_key(session_id), ttl, json.dumps(payload, ensure_ascii=False))
    client.sadd(_user_sessions_key(user_id), session_id)
    client.expire(_user_sessions_key(user_id), ttl)
    return session_id, payload, csrf_token


def read_session(session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    raw = cache_redis().get(_session_key(session_id))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        delete_session(session_id)
        return None
    if not isinstance(payload, dict) or "user_id" not in payload:
        delete_session(session_id)
        return None
    return payload


def touch_session(session_id: str, payload: dict[str, Any]) -> None:
    """滑动续期：仅在剩余 TTL 不足一半时写回，避免每个请求都写 Redis。"""
    if not session_id:
        return
    client = cache_redis()
    ttl = client.ttl(_session_key(session_id))
    if ttl is None or ttl < 0:
        return
    if ttl > settings.session_ttl_seconds // 2:
        return
    payload = dict(payload)
    payload["last_seen"] = int(utc_now().timestamp())
    client.setex(_session_key(session_id), settings.session_ttl_seconds, json.dumps(payload, ensure_ascii=False))


def delete_session(session_id: str, user_id: int | None = None) -> None:
    if not session_id:
        return
    client = cache_redis()
    payload = read_session(session_id)
    client.delete(_session_key(session_id))
    target_user = user_id or (int(payload["user_id"]) if payload else None)
    if target_user:
        client.srem(_user_sessions_key(target_user), session_id)


def revoke_user_sessions(user_id: int) -> int:
    """退出、停用、修改密码时撤销该用户全部会话。"""
    client = cache_redis()
    ids = client.smembers(_user_sessions_key(user_id))
    count = 0
    if ids:
        keys = [_session_key(item) for item in ids]
        client.delete(*keys)
        count = len(keys)
    client.delete(_user_sessions_key(user_id))
    return count


def issue_anonymous_csrf(ttl_seconds: int = 7200) -> str:
    """登录前获取 CSRF 令牌：Redis 记录一次性存在性。"""
    token = new_csrf_token()
    cache_redis().setex(f"{CSRF_KEY_PREFIX}{token}", ttl_seconds, "1")
    return token


def anonymous_csrf_exists(token: str) -> bool:
    if not token:
        return False
    return bool(cache_redis().exists(f"{CSRF_KEY_PREFIX}{token}"))


def consume_anonymous_csrf(token: str) -> bool:
    if not token:
        return False
    client = cache_redis()
    key = f"{CSRF_KEY_PREFIX}{token}"
    if not client.get(key):
        return False
    client.delete(key)
    return True


def cache_get_json(key: str) -> Any | None:
    raw = cache_redis().get(f"{CACHE_KEY_PREFIX}{key}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def cache_set_json(key: str, value: Any, ttl_seconds: int = 300) -> None:
    cache_redis().setex(f"{CACHE_KEY_PREFIX}{key}", ttl_seconds, json.dumps(value, ensure_ascii=False))


def cache_delete(key: str) -> None:
    cache_redis().delete(f"{CACHE_KEY_PREFIX}{key}")


def acquire_lock(name: str, ttl_seconds: int = 60) -> str | None:
    """Redis 分布式锁（只用于协调，最终防重依赖数据库唯一约束）。"""
    token = new_session_id()
    ok = cache_redis().set(f"vs:lock:{name}", token, nx=True, ex=ttl_seconds)
    return token if ok else None


def release_lock(name: str, token: str) -> None:
    client = cache_redis()
    key = f"vs:lock:{name}"
    if client.get(key) == token:
        client.delete(key)


def ensure_cache_available() -> None:
    try:
        cache_redis().ping()
    except Exception as exc:  # noqa: BLE001 - 连接类异常统一转 503
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "缓存服务不可用") from exc
