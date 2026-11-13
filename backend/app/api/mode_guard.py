"""部署形态边界守卫：HTTP 依赖 / WebSocket 依赖 / 纯 ASGI 中间件。

* ``ModeBoundaryMiddleware``：纯 ASGI 中间件，HTTP 与 WebSocket 统一处理。
  ``deployment_mode=tenant`` 时拒绝 ``/api/v1/platform/*``、``/api/v1/agent/*``
  （含平台 SSH WebSocket ``/platform/nodes/{id}/ssh/ws``），返回统一 404，
  不暴露平台功能；``/healthz``、``/readyz``、认证与租户业务接口放行。
* ``http_mode_guard``：同一规则的依赖版本（路由级兜底，便于未注册中间件的测试环境）。
* ``http_tenant_binding_guard``：固定实例租户绑定，校验路径参数 / 查询参数 /
  JSON 请求体中的 ``tenant_id`` 必须等于 ``settings.tenant_id``，不一致 403。
* ``ws_tenant_mode_guard``：WebSocket 路由依赖（供平台 SSH 终端等 WS 路由显式引用）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request, WebSocket
from fastapi.exceptions import WebSocketException
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.http import error_body
from app.core.mode import (
    ensure_payload_tenant_id,
    ensure_tenant_id_matches,
    fixed_tenant_id,
    mode_allows_path,
    platform_only_path,
)

logger = logging.getLogger("ventasync.mode")

MODE_DENIED_MESSAGE = "当前实例未开放该接口"
WS_DENIED_CODE = 4404


class ModeBoundaryMiddleware:
    """按部署形态拒绝平台 / Agent 接口的纯 ASGI 中间件（覆盖 HTTP 与 WebSocket）。

    只处理 ``deployment_mode=tenant``：中心模式与网关模式的行为由现有逻辑负责，
    避免改变已上线部署的语义。健康检查与静态资源不受影响。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") in {"http", "websocket"} and settings.deployment_mode == "tenant":
            path = str(scope.get("path") or "")
            if platform_only_path(path):
                logger.warning("租户模式拒绝平台接口：type=%s path=%s", scope.get("type"), path)
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": WS_DENIED_CODE})
                else:
                    response = JSONResponse(
                        status_code=404,
                        content=error_body(ErrorCode.NOT_FOUND.value, MODE_DENIED_MESSAGE),
                    )
                    await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def http_mode_guard(request: Request) -> None:
    """路由级兜底：部署形态不允许的路径统一 404。"""
    if not mode_allows_path(settings.deployment_mode, request.url.path):
        raise AppError(ErrorCode.NOT_FOUND, MODE_DENIED_MESSAGE)


def _json_body(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        try:
            return json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    return value


async def http_tenant_binding_guard(request: Request) -> None:
    """固定实例租户绑定：路径 / 查询 / JSON 请求体中的 tenant_id 必须与实例一致。

    非 tenant 模式直接放行；tenant 模式未配置 TENANT_ID 时，只要请求涉及 tenant_id
    就返回 503（配置错误，需部署方修复），避免误用其他租户数据。
    """
    if settings.deployment_mode != "tenant":
        return

    path_tenant = None
    try:
        path_tenant = request.path_params.get("tenant_id")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - path_params 不可用时忽略
        path_tenant = None
    if path_tenant is not None:
        ensure_tenant_id_matches(int(path_tenant) if str(path_tenant).isdigit() else None)

    for value in request.query_params.getlist("tenant_id"):
        if str(value).strip().isdigit():
            ensure_tenant_id_matches(int(str(value).strip()))

    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            raw = await request.body()
        except Exception:  # noqa: BLE001 - 读取请求体失败交给后续校验
            raw = b""
        payload = _json_body(raw)
        if payload is not None:
            ensure_payload_tenant_id(payload)


async def ws_tenant_mode_guard(websocket: WebSocket) -> None:
    """WebSocket 路由依赖：部署形态不允许的路径直接以 4404 关闭连接。"""
    if not mode_allows_path(settings.deployment_mode, websocket.url.path):
        raise WebSocketException(code=WS_DENIED_CODE, reason=MODE_DENIED_MESSAGE)


def tenant_binding_hint() -> dict[str, Any]:
    """自检提示：tenant 模式下展示固定租户是否已配置（不含敏感信息）。"""
    configured = settings.deployment_mode != "tenant" or fixed_tenant_id() > 0
    return {"tenant_binding_configured": configured, "mode": settings.deployment_mode}


__all__ = [
    "MODE_DENIED_MESSAGE",
    "ModeBoundaryMiddleware",
    "http_mode_guard",
    "http_tenant_binding_guard",
    "tenant_binding_hint",
    "ws_tenant_mode_guard",
]
