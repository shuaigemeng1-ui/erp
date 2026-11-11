"""登录失败限流：同一邮箱 + IP 连续失败 5 次 / 15 分钟返回 429。

实现约定（见 plan.txt §3「登录与接口约定」）：

* 使用 ``app/core/redis.py`` 的 ``cache_redis()``，键含业务前缀与摘要，不在键中暴露明文邮箱；
* 成功登录清零；失败计数只统计「账号或密码错误」，账号停用不计数；
* Redis 不可用时**降级放行**并记录 warning：限流是防护增强，不能让登录入口整体不可用；
* 达到阈值抛 ``AUTH_RATE_LIMITED``（429），并带 ``Retry-After`` 响应头。
"""

from __future__ import annotations

import logging

from app.core.errors import AppError, ErrorCode
from app.core.redis import cache_redis
from app.core.security import sha256_hex

logger = logging.getLogger(__name__)

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_KEY_PREFIX = "vs:login:fail:"


def login_failure_key(email: str, ip: str = "") -> str:
    """失败计数键：邮箱 + IP 的摘要，避免明文邮箱进入 Redis 键。"""
    normalized = (email or "").strip().lower()
    digest = sha256_hex(f"{normalized}|{(ip or '').strip()}")[:32]
    return f"{LOGIN_KEY_PREFIX}{digest}"


def is_limited(count: int) -> bool:
    """连续失败次数是否达到限流阈值（纯函数）。"""
    return int(count) >= LOGIN_MAX_FAILURES


def retry_after_seconds(ttl_seconds: int | None) -> int:
    """把剩余 TTL 规范化为 ``Retry-After`` 秒数（无 TTL 时退回窗口长度）。"""
    if not ttl_seconds or ttl_seconds <= 0:
        return LOGIN_WINDOW_SECONDS
    return int(ttl_seconds)


def ensure_login_allowed(email: str, ip: str = "") -> None:
    """登录前检查是否已被限流；Redis 异常时降级放行。"""
    key = login_failure_key(email, ip)
    try:
        client = cache_redis()
        raw = client.get(key)
        if raw is None or not is_limited(int(raw)):
            return
        ttl = client.ttl(key)
    except Exception:  # noqa: BLE001 - 限流组件故障不阻断登录
        logger.warning("登录限流检查失败，已降级放行 email_mask=%s", _mask_email(email), exc_info=True)
        return
    headers = {"Retry-After": str(retry_after_seconds(ttl))}
    raise AppError(
        ErrorCode.AUTH_RATE_LIMITED,
        f"登录失败次数过多，请 {retry_after_seconds(ttl) // 60 or 1} 分钟后再试",
        headers=headers,
    )


def record_login_failure(email: str, ip: str = "") -> int | None:
    """记录一次登录失败（INCR + 首次设置过期时间），返回当前失败次数。"""
    key = login_failure_key(email, ip)
    try:
        client = cache_redis()
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, LOGIN_WINDOW_SECONDS)
        count = int(pipe.execute()[0])
        if is_limited(count):
            logger.warning("登录失败次数达到阈值 email_mask=%s", _mask_email(email))
        return count
    except Exception:  # noqa: BLE001 - 计数失败不影响登录错误返回
        logger.warning("登录失败计数失败 email_mask=%s", _mask_email(email), exc_info=True)
        return None


def clear_login_failures(email: str, ip: str = "") -> None:
    """登录成功后清零失败计数。"""
    try:
        cache_redis().delete(login_failure_key(email, ip))
    except Exception:  # noqa: BLE001 - 清理失败不影响登录成功
        logger.warning("登录失败计数清理失败 email_mask=%s", _mask_email(email), exc_info=True)


def _mask_email(email: str) -> str:
    """日志脱敏：保留首字符与域名。"""
    normalized = (email or "").strip().lower()
    if "@" not in normalized:
        return "***"
    local, _, domain = normalized.partition("@")
    return f"{local[:1]}***@{domain}"
