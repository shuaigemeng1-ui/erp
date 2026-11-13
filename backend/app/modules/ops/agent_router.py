"""Agent 注册与指标上报接口（节点令牌认证，不走平台管理员会话）。"""

from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import FileResponse

from app.api.deps import CenterDb
from app.core.http import success
from app.modules.ops import service
from app.modules.ops.schemas import AgentJobEvent, NodeEnrollIn, TelemetryPayload

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


@router.post("/jobs/poll", summary="Agent 领取运维任务")
def poll_job(center: CenterDb, authorization: str | None = Header(default=None)) -> dict:
    token = _bearer_token(authorization)
    if not token:
        from app.core.errors import AppError, ErrorCode
        raise AppError(ErrorCode.AUTH_SESSION_REQUIRED, "节点令牌无效")
    node = service._node_from_token(center, token)
    return success({"job": service.poll_agent_job(center, node)})


@router.post("/jobs/{job_id}/events", summary="Agent 上报任务阶段/结果")
def job_event(center: CenterDb, job_id: int, payload: AgentJobEvent, authorization: str | None = Header(default=None)) -> dict:
    token = _bearer_token(authorization)
    if not token:
        from app.core.errors import AppError, ErrorCode
        raise AppError(ErrorCode.AUTH_SESSION_REQUIRED, "节点令牌无效")
    node = service._node_from_token(center, token)
    return success(service.agent_job_event(center, node, job_id, payload))


@router.get("/ping", summary="Agent 连通性检查")
def ping(request: Request) -> dict:
    return success({"ok": True})


@router.get("/releases/{version}/bundle", summary="Agent 下载离线镜像包")
def download_release_bundle(
    version: str,
    center: CenterDb,
    authorization: str | None = Header(default=None),
) -> FileResponse:
    """节点凭令牌下载中心构建的版本镜像 tar.gz，用于本机 docker load。"""
    import re
    from pathlib import Path
    from app.core.config import settings
    from app.core.errors import AppError, ErrorCode

    token = _bearer_token(authorization)
    node = service._node_from_token(center, token) if token else None
    if node is None:
        raise AppError(ErrorCode.AUTH_SESSION_REQUIRED, "节点令牌无效")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", version or ""):
        raise AppError(ErrorCode.VALIDATION_ERROR, "版本号格式不正确")
    path = Path(settings.release_bundle_dir) / f"{version}.tar.gz"
    if not path.is_file():
        raise AppError(ErrorCode.NOT_FOUND, "该版本离线镜像包不存在")
    return FileResponse(path, media_type="application/gzip", filename=f"erp-{version}.tar.gz")
