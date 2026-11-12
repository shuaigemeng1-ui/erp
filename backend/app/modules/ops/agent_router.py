"""Agent 注册与指标上报接口（节点令牌认证，不走平台管理员会话）。"""

from __future__ import annotations

from fastapi import APIRouter, Header, Request

from app.api.deps import CenterDb
from app.core.http import success
from app.modules.ops import service
from app.modules.ops.schemas import NodeEnrollIn, TelemetryPayload

router = APIRouter(prefix="/agent", tags=["agent"])


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


@router.post("/enroll", summary="Agent 使用一次性令牌注册节点")
def enroll(center: CenterDb, payload: NodeEnrollIn) -> dict:
    return success(service.enroll_node(center, payload))


@router.post("/telemetry", summary="Agent 上报主机与业务指标")
def telemetry(
    center: CenterDb,
    payload: TelemetryPayload,
    authorization: str | None = Header(default=None),
) -> dict:
    token = _bearer_token(authorization)
    node = service._node_from_token(center, token) if token else None
    if node is None:
        from app.core.errors import AppError, ErrorCode

        raise AppError(ErrorCode.AUTH_SESSION_REQUIRED, "节点令牌无效")
    return success(service.ingest_telemetry(center, node, payload))


@router.get("/ping", summary="Agent 连通性检查")
def ping(request: Request) -> dict:
    return success({"ok": True})
