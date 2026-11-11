"""结构化日志配置。

日志字段固定包含 ``request_id``、``tenant_id``、``user_id``，便于跨租户问题排查。
禁止在日志中输出明文密码、Token、Shopee 应用 Secret。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.core.context import get_current_user_id, get_request_id, get_tenant_context

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """生产环境 JSON 行日志，便于采集与检索。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        try:
            payload["request_id"] = get_request_id()
            payload["user_id"] = get_current_user_id()
            tenant = get_tenant_context(required=False)
            payload["tenant_id"] = tenant.tenant_id if tenant else None
        except Exception:  # noqa: BLE001 - 日志不能因上下文异常而失败
            payload["request_id"] = None
        extra = {key: value for key, value in record.__dict__.items() if key not in _RESERVED}
        if extra:
            payload["extra"] = extra
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        try:
            tenant = get_tenant_context(required=False)
            tenant_part = f" tenant={tenant.tenant_id}" if tenant else ""
            return f"{base} request_id={get_request_id()}{tenant_part}"
        except Exception:  # noqa: BLE001
            return base


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    if settings.is_production:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            TextFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    # 降低第三方库噪音
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
