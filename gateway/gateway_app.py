#!/usr/bin/env python3
"""ERP 独立集成网关启动器。

网关实现位于 ``backend/app/modules/gateway/``，本启动器负责定位后端包路径后创建应用；
独立镜像内后端包复制到 ``/app/app``，本地源码运行时使用 ``../backend``。

环境变量（全部由网关容器注入，不读取管理中心配置）：
  GATEWAY_DB_PATH=/data/gateway.db
  GATEWAY_SECRET_KEY=<32+ 随机字符，仅网关持有>
  GATEWAY_ADMIN_TOKEN=<平台管理令牌，仅网关与管理中心侧使用>
  GATEWAY_CHANNELS=shopee,tiktok
  GATEWAY_ENVIRONMENTS=production,test
  GATEWAY_INSTANCE_AUTH_ENABLED=true
  GATEWAY_LEGACY_TENANT_ADMIN_ENABLED=true
  GATEWAY_LEGACY_OAUTH_FALLBACK_ENABLED=true
  GATEWAY_RATE_LIMIT_PER_MINUTE=120
  GATEWAY_OAUTH_STATE_TTL_SECONDS=600
  GATEWAY_REFRESH_LOCK_TTL_SECONDS=120
  GATEWAY_DELIVERY_MAX_ATTEMPTS=5

运行：``uvicorn gateway_app:app --host 0.0.0.0 --port 8100``（在 /app 或 gateway/ 目录）。
"""

from __future__ import annotations

import os
import sys


def _bootstrap_backend_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("GATEWAY_BACKEND_PATH", "").strip(),
        os.path.join(os.path.dirname(here), "backend"),
        os.path.join(here, "backend"),
        here,
    ]
    for candidate in candidates:
        if not candidate:
            continue
        marker = os.path.join(candidate, "app", "modules", "gateway", "app.py")
        if os.path.isfile(marker):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return candidate
    raise RuntimeError(
        "未找到网关后端包：请设置 GATEWAY_BACKEND_PATH 或使用仓库根目录构建镜像"
    )


_bootstrap_backend_path()

from app.modules.gateway.app import create_app  # noqa: E402

app = create_app()
