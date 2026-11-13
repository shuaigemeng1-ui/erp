"""独立集成网关后端包：实例身份、OAuth state、刷新锁、限流与幂等交付。

本包只依赖标准库 + FastAPI/Pydantic/cryptography，不 import ``app.core`` / ``app.db`` /
管理中心业务模块，便于网关容器独立部署；租户侧客户端 :mod:`app.modules.gateway.client`
供 ``app.modules.channels`` 在配置 ``GATEWAY_CHANNEL_CONFIG=true`` 时使用。
"""

from __future__ import annotations

__all__ = ["config", "security", "store", "app", "client"]
