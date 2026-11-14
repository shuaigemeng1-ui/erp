"""独立集成网关（网关进程配置与常量）。

约束（批次4「独立集成网关与实例身份」）：

* 网关可在管理中心之外独立部署：配置来自环境变量，持久化只使用本地 SQLite，
  **不 import** ``app.core`` / ``app.db`` / ``app.modules.*`` / Celery / Redis；
* 共享主密钥（``GATEWAY_SECRET_KEY``）与平台管理员令牌（``GATEWAY_ADMIN_TOKEN``）
  只存在于网关进程；租户实例只持有一次性发放的实例令牌（只存 HMAC 摘要）；
* 所有迁移/回滚开关都落 SQLite ``gateway_settings``，重启后保持。

本包只依赖 Python 标准库 + FastAPI/Pydantic/cryptography，便于独立镜像部署。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field

DEFAULT_CHANNELS = ("shopee", "tiktok")
DEFAULT_ENVIRONMENTS = ("production", "test")

# 实例令牌前缀（便于日志/配置识别；真实令牌只返回一次）
INSTANCE_TOKEN_PREFIX = "gw_inst_"
# 令牌/state 摘要 HMAC 用途域，防止跨用途复用
TOKEN_HASH_CONTEXT = "gateway-instance-token"
STATE_HASH_CONTEXT = "gateway-oauth-state"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_list(name: str, default: Iterable[str]) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    items = tuple(item.strip().lower() for item in raw.split(",") if item.strip())
    return items or tuple(default)


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    raw = os.environ.get(name, default).strip().upper()
    return raw if raw in allowed else default


@dataclass(frozen=True)
class GatewayConfig:
    """网关运行配置（测试可直接构造，不读环境变量）。"""

    db_path: str = "/data/gateway.db"
    # 留空=SQLite（db_path）；postgresql://...=多实例共享 PostgreSQL（HA 部署）
    database_url: str = ""
    secret_key: str = ""
    admin_token: str = ""
    channels: tuple[str, ...] = DEFAULT_CHANNELS
    environments: tuple[str, ...] = DEFAULT_ENVIRONMENTS
    # 迁移开关默认值：新链开启、legacy 保留（验证通过后再关闭）
    instance_auth_enabled: bool = True
    legacy_tenant_admin_enabled: bool = True
    legacy_oauth_fallback_enabled: bool = True
    legacy_default_callback: str = ""
    # 一次性 state / 刷新锁 / 限流 / 幂等交付
    oauth_state_ttl_seconds: int = 600
    refresh_lock_ttl_seconds: int = 120
    rate_limit_per_minute: int = 120
    delivery_max_attempts: int = 5
    delivery_retry_backoff_seconds: int = 30
    # 新链验证窗口：实例最近一次真实调用须在窗口内才允许 verify
    verify_window_seconds: int = 600
    sqlite_busy_timeout_ms: int = 5000
    # 状态/锁清理：过期记录可在健康检查时顺手清理
    purge_expired_on_health: bool = True
    webhook_relay_secret: str = ""
    webhook_dispatch_enabled: bool = True
    webhook_max_body_bytes: int = 1024 * 1024
    webhook_dispatch_interval_seconds: int = 2
    webhook_max_attempts: int = 10
    # SQLite 持久性：WAL + synchronous=FULL（默认，接收延迟换持久性；测试/边缘环境可改 NORMAL）
    sqlite_synchronous: str = "FULL"
    # Webhook 分发并发与租约（批次4 task-41）
    webhook_dispatch_global_concurrency: int = 64
    webhook_dispatch_tenant_concurrency: int = 32
    webhook_dispatch_store_concurrency: int = 1
    webhook_claim_batch_size: int = 200
    webhook_lease_seconds: int = 60
    webhook_delivery_timeout_seconds: int = 10
    webhook_retry_base_seconds: int = 5
    webhook_retry_max_seconds: int = 3600
    webhook_retry_jitter_percent: int = 20
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> GatewayConfig:
        return cls(
            db_path=os.environ.get("GATEWAY_DB_PATH", "/data/gateway.db"),
            database_url=os.environ.get("GATEWAY_DATABASE_URL", "").strip(),
            secret_key=os.environ.get("GATEWAY_SECRET_KEY", ""),
            admin_token=os.environ.get("GATEWAY_ADMIN_TOKEN", ""),
            channels=_env_list("GATEWAY_CHANNELS", DEFAULT_CHANNELS),
            environments=_env_list("GATEWAY_ENVIRONMENTS", DEFAULT_ENVIRONMENTS),
            instance_auth_enabled=_env_bool("GATEWAY_INSTANCE_AUTH_ENABLED", True),
            legacy_tenant_admin_enabled=_env_bool(
                "GATEWAY_LEGACY_TENANT_ADMIN_ENABLED", True
            ),
            legacy_oauth_fallback_enabled=_env_bool(
                "GATEWAY_LEGACY_OAUTH_FALLBACK_ENABLED", True
            ),
            legacy_default_callback=os.environ.get(
                "GATEWAY_DEFAULT_TENANT_CALLBACK", ""
            ).strip(),
            oauth_state_ttl_seconds=_env_int("GATEWAY_OAUTH_STATE_TTL_SECONDS", 600, 30),
            refresh_lock_ttl_seconds=_env_int("GATEWAY_REFRESH_LOCK_TTL_SECONDS", 120, 10),
            rate_limit_per_minute=_env_int("GATEWAY_RATE_LIMIT_PER_MINUTE", 120, 1),
            delivery_max_attempts=_env_int("GATEWAY_DELIVERY_MAX_ATTEMPTS", 5, 1),
            delivery_retry_backoff_seconds=_env_int(
                "GATEWAY_DELIVERY_RETRY_BACKOFF_SECONDS", 30, 0
            ),
            verify_window_seconds=_env_int("GATEWAY_VERIFY_WINDOW_SECONDS", 600, 30),
            sqlite_busy_timeout_ms=_env_int("GATEWAY_SQLITE_BUSY_TIMEOUT_MS", 5000, 100),
            purge_expired_on_health=_env_bool("GATEWAY_PURGE_EXPIRED_ON_HEALTH", True),
            webhook_relay_secret=os.environ.get("GATEWAY_WEBHOOK_RELAY_SECRET", "").strip(),
            webhook_dispatch_enabled=_env_bool("GATEWAY_WEBHOOK_DISPATCH_ENABLED", True),
            webhook_max_body_bytes=_env_int("GATEWAY_WEBHOOK_MAX_BODY_BYTES", 1024 * 1024, 1024),
            webhook_dispatch_interval_seconds=_env_int(
                "GATEWAY_WEBHOOK_DISPATCH_INTERVAL_SECONDS", 2, 1
            ),
            webhook_max_attempts=_env_int("GATEWAY_WEBHOOK_MAX_ATTEMPTS", 10, 1),
            sqlite_synchronous=_env_choice(
                "GATEWAY_SQLITE_SYNCHRONOUS", "FULL", {"NORMAL", "FULL"}
            ),
            webhook_dispatch_global_concurrency=_env_int(
                "GATEWAY_WEBHOOK_DISPATCH_GLOBAL_CONCURRENCY", 64, 1
            ),
            webhook_dispatch_tenant_concurrency=_env_int(
                "GATEWAY_WEBHOOK_DISPATCH_TENANT_CONCURRENCY", 32, 1
            ),
            webhook_dispatch_store_concurrency=_env_int(
                "GATEWAY_WEBHOOK_DISPATCH_STORE_CONCURRENCY", 1, 1
            ),
            webhook_claim_batch_size=_env_int(
                "GATEWAY_WEBHOOK_CLAIM_BATCH_SIZE", 200, 1
            ),
            webhook_lease_seconds=_env_int("GATEWAY_WEBHOOK_LEASE_SECONDS", 60, 5),
            webhook_delivery_timeout_seconds=_env_int(
                "GATEWAY_WEBHOOK_DELIVERY_TIMEOUT_SECONDS", 10, 1
            ),
            webhook_retry_base_seconds=_env_int(
                "GATEWAY_WEBHOOK_RETRY_BASE_SECONDS", 5, 1
            ),
            webhook_retry_max_seconds=_env_int(
                "GATEWAY_WEBHOOK_RETRY_MAX_SECONDS", 3600, 1
            ),
            webhook_retry_jitter_percent=_env_int(
                "GATEWAY_WEBHOOK_RETRY_JITTER_PERCENT", 20, 0
            ),
        )

    def validate(self) -> None:
        if not self.secret_key:
            raise ValueError("GATEWAY_SECRET_KEY 不能为空（用于令牌摘要与渠道密钥加密）")
        if not self.channels:
            raise ValueError("GATEWAY_CHANNELS 不能为空")
        if not self.environments:
            raise ValueError("GATEWAY_ENVIRONMENTS 不能为空")
        if str(self.sqlite_synchronous).upper() not in {"NORMAL", "FULL"}:
            raise ValueError("GATEWAY_SQLITE_SYNCHRONOUS 只能是 NORMAL 或 FULL")
        if self.database_url and not self.database_url.startswith(
            ("postgresql://", "postgres://", "sqlite:")
        ):
            raise ValueError("GATEWAY_DATABASE_URL 只支持 postgresql:// 或 sqlite: 前缀")
        if self.webhook_dispatch_global_concurrency < 1:
            raise ValueError("webhook_dispatch_global_concurrency 必须 >= 1")
        if self.webhook_dispatch_tenant_concurrency < 1:
            raise ValueError("webhook_dispatch_tenant_concurrency 必须 >= 1")
        if self.webhook_dispatch_store_concurrency < 1:
            raise ValueError("webhook_dispatch_store_concurrency 必须 >= 1")
        if self.webhook_retry_max_seconds < self.webhook_retry_base_seconds:
            raise ValueError("webhook_retry_max_seconds 不能小于 webhook_retry_base_seconds")
        if self.webhook_lease_seconds <= self.webhook_delivery_timeout_seconds:
            raise ValueError("webhook_lease_seconds 必须大于 webhook_delivery_timeout_seconds，避免重复投递")

    def channel_supported(self, channel: str) -> bool:
        return (channel or "").strip().lower() in self.channels

    def environment_supported(self, environment: str) -> bool:
        return (environment or "").strip().lower() in self.environments


__all__ = [
    "DEFAULT_CHANNELS",
    "DEFAULT_ENVIRONMENTS",
    "GatewayConfig",
    "INSTANCE_TOKEN_PREFIX",
    "STATE_HASH_CONTEXT",
    "TOKEN_HASH_CONTEXT",
]
