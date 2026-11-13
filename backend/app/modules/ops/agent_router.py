"""Agent 注册、心跳、任务租约与指标上报接口（节点令牌认证，不走平台管理员会话）。

协议实现见 ``app.modules.ops.agent_protocol``；本文件只做 Bearer 认证、请求模型与响应包装。
旧 Agent 端点（enroll/ping/telemetry 单条/任务事件旧字段）保持兼容。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Path, Request
from fastapi.responses import FileResponse

from app.api.deps import CenterDb
from app.core.http import success
from app.modules.ops import agent_protocol
from app.modules.ops import service
from app.modules.ops.agent_protocol import (
    JobEventIn,
    JobHeartbeatIn,
    JobPollIn,
    JobReconcileIn,
    NodeHeartbeatIn,
)
from app.modules.ops.schemas import NodeEnrollIn

router = APIRouter(prefix="/agent", tags=["agent"])


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


def _header_int(value: int | str | None) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _auth_node(center, authorization: str | None):
    return agent_protocol.authenticate_node(center, _bearer_token(authorization))


@router.post("/enroll", summary="Agent 使用一次性令牌注册节点")
def enroll(center: CenterDb, payload: NodeEnrollIn) -> dict:
    return success(service.enroll_node(center, payload))


@router.post("/heartbeat", summary="Agent 协议心跳（版本/能力/启动批次）")
def heartbeat(
    center: CenterDb,
    payload: NodeHeartbeatIn,
    authorization: str | None = Header(default=None),
) -> dict:
    node = _auth_node(center, authorization)
    return success(agent_protocol.node_heartbeat(center, node, payload))


@router.post("/telemetry", summary="Agent 上报主机与业务指标（支持有限批量）")
def telemetry(
    center: CenterDb,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict:
    node = _auth_node(center, authorization)
    return success(agent_protocol.ingest_telemetry(center, node, payload))


@router.post("/jobs/poll", summary="Agent 领取运维任务（租约 + 执行批次）")
def poll_job(
    center: CenterDb,
    payload: JobPollIn | None = None,
    authorization: str | None = Header(default=None),
    x_agent_protocol: int | None = Header(default=None, alias="X-Agent-Protocol"),
) -> dict:
    node = _auth_node(center, authorization)
    return success(
        agent_protocol.poll_job(
            center,
            node,
            payload,
            claimed_protocol=_header_int(x_agent_protocol),
        )
    )


@router.post("/jobs/{job_id}/heartbeat", summary="Agent 任务执行心跳（续租 120s）")
def job_heartbeat(
    center: CenterDb,
    payload: JobHeartbeatIn,
    job_id: int = Path(ge=1),
    authorization: str | None = Header(default=None),
) -> dict:
    node = _auth_node(center, authorization)
    return success(agent_protocol.job_heartbeat(center, node, job_id, payload))


@router.post("/jobs/{job_id}/events", summary="Agent 上报任务阶段/结果（event_seq 幂等）")
def job_event(
    center: CenterDb,
    payload: JobEventIn,
    job_id: int = Path(ge=1),
    authorization: str | None = Header(default=None),
) -> dict:
    node = _auth_node(center, authorization)
    return success(agent_protocol.job_event(center, node, job_id, payload))


@router.post("/jobs/{job_id}/reconcile", summary="Agent 上报本地执行批次/结果并对账")
def job_reconcile(
    center: CenterDb,
    payload: JobReconcileIn,
    job_id: int = Path(ge=1),
    authorization: str | None = Header(default=None),
) -> dict:
    node = _auth_node(center, authorization)
    return success(agent_protocol.reconcile_job(center, node, job_id, payload))


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

    node = _auth_node(center, authorization)
    del node  # 仅校验令牌
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", version or ""):
        raise AppError(ErrorCode.VALIDATION_ERROR, "版本号格式不正确")
    path = Path(settings.release_bundle_dir) / f"{version}.tar.gz"
    if not path.is_file():
        raise AppError(ErrorCode.NOT_FOUND, "该版本离线镜像包不存在")
    return FileResponse(path, media_type="application/gzip", filename=f"erp-{version}.tar.gz")
