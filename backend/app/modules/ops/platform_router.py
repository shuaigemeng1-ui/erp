"""平台管理员：服务器与实例运维接口。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect

from app.api.deps import CenterDb, PlatformAdmin
from app.core.context import get_request_id
from app.core.errors import AppError
from app.core.http import PageParams, paged, success
from app.modules.ops import service
from app.modules.ops.monitoring_router import router as monitoring_router
from app.modules.ops.schemas import (
    AlertAckIn,
    InstanceCreate,
    InstanceUpdate,
    MaintenanceWindowCreate,
    NodeSshConfigIn,
    NodeSshExecIn,
    NodeSshFingerprintResetIn,
    OperationCreate,
    BackupRestoreCheckIn,
    ReleaseCreate,
    ReleaseUpdate,
)

logger = logging.getLogger("ventasync.ssh")

router = APIRouter(prefix="/platform", tags=["platform-ops"])
# 监控总览路由独立成模块（task-29），这里只做一行接线，避免多人改大文件。
router.include_router(monitoring_router)


def _client_ip(request: Request | None) -> str:
    if request is None or request.client is None:
        return ""
    return request.client.host or ""


@router.get("/instances", summary="租户实例列表")
def list_instances(
    center: CenterDb,
    _: PlatformAdmin,
    pagination: PageParams = Depends(),
    q: str = Query(default="", max_length=100),
    status: str = Query(default="", max_length=32),
    deployment_mode: str = Query(default="", max_length=16, description="部署模式过滤：shared/independent"),
    migration_status: str = Query(
        default="", max_length=16, description="迁移状态过滤：registered/migrating/migrated/rollback"
    ),
) -> dict:
    data = service.list_instances(
        center,
        page=pagination.page,
        page_size=pagination.page_size,
        q=q,
        status=status,
        deployment_mode=deployment_mode,
        migration_status=migration_status,
    )
    return paged(data["items"], data["total"], data["page"], data["page_size"])


@router.post("/instances", summary="登记租户实例")
def create_instance(center: CenterDb, admin: PlatformAdmin, payload: InstanceCreate) -> dict:
    return success(service.create_instance(center, payload, actor_user_id=admin.user_id))


@router.get("/instances/{instance_id}", summary="租户实例详情")
def get_instance(center: CenterDb, _: PlatformAdmin, instance_id: int) -> dict:
    instance = service.get_instance(center, instance_id)
    from app.modules.identity.models_center import Tenant

    tenant = center.get(Tenant, instance.tenant_id)
    return success(service.instance_payload(center, instance, tenant.name if tenant else ""))


@router.patch("/instances/{instance_id}", summary="更新租户实例")
def update_instance(
    center: CenterDb,
    admin: PlatformAdmin,
    instance_id: int,
    payload: InstanceUpdate,
) -> dict:
    return success(service.update_instance(center, instance_id, payload, actor_user_id=admin.user_id))


@router.get("/instances/{instance_id}/nodes", summary="实例节点列表")
def list_nodes(center: CenterDb, _: PlatformAdmin, instance_id: int) -> dict:
    return success(service.list_nodes(center, instance_id))


@router.post("/instances/{instance_id}/nodes", summary="生成服务器接入令牌")
def create_node_enrollment(
    center: CenterDb, admin: PlatformAdmin, instance_id: int
) -> dict:
    return success(service.create_node_enrollment(center, instance_id, actor_user_id=admin.user_id))


@router.post("/nodes/{node_id}/probe", summary="立即探测节点公网访问")
def probe_node(center: CenterDb, _: PlatformAdmin, node_id: int) -> dict:
    return success(service.probe_node(center, node_id))


@router.get("/nodes/{node_id}/ssh", summary="读取服务器 SSH 配置")
def get_node_ssh(center: CenterDb, _: PlatformAdmin, node_id: int) -> dict:
    return success(service.get_ssh_config(center, node_id))


@router.put("/nodes/{node_id}/ssh", summary="保存服务器 SSH 账号")
def save_node_ssh(
    center: CenterDb,
    admin: PlatformAdmin,
    node_id: int,
    payload: NodeSshConfigIn,
    request: Request,
) -> dict:
    return success(
        service.save_ssh_config(
            center,
            node_id,
            payload,
            actor_user_id=admin.user_id,
            request_id=get_request_id(),
            ip=_client_ip(request),
        )
    )


@router.post("/nodes/{node_id}/ssh/fingerprint/reset", summary="重置服务器 SSH 主机指纹")
def reset_node_ssh_fingerprint(
    center: CenterDb,
    admin: PlatformAdmin,
    node_id: int,
    payload: NodeSshFingerprintResetIn,
    request: Request,
) -> dict:
    """管理员确认后清空主机指纹；下次连接重新记录（用于服务器重装/换机）。"""
    del payload  # 已由模型校验 confirm=true
    return success(
        service.reset_ssh_host_fingerprint(
            center,
            node_id,
            actor_user_id=admin.user_id,
            request_id=get_request_id(),
            ip=_client_ip(request),
        )
    )


@router.post("/nodes/{node_id}/ssh/exec", summary="通过 SSH 执行命令")
def exec_node_ssh(
    center: CenterDb,
    admin: PlatformAdmin,
    node_id: int,
    payload: NodeSshExecIn,
    request: Request,
) -> dict:
    return success(
        service.ssh_execute(
            center,
            node_id,
            payload.command,
            actor_user_id=admin.user_id,
            request_id=get_request_id(),
            ip=_client_ip(request),
        )
    )


@router.get("/nodes/{node_id}/metrics", summary="节点指标趋势")
def node_metrics(
    center: CenterDb,
    _: PlatformAdmin,
    node_id: int,
    minutes: int = Query(default=60, ge=1, le=43200),
) -> dict:
    return success(service.list_metrics(center, node_id, minutes=minutes))


@router.get("/nodes/{node_id}/aggregates", summary="节点指标聚合趋势")
def node_aggregates(
    center: CenterDb,
    _: PlatformAdmin,
    node_id: int,
    resolution: str = Query(default="5m", pattern="^(5m|1h)$"),
    hours: int = Query(default=24, ge=1, le=720),
) -> dict:
    return success(service.list_metric_aggregates(center, node_id, resolution=resolution, hours=hours))


@router.get("/nodes/{node_id}/checks", summary="节点公网探测记录")
def node_checks(
    center: CenterDb,
    _: PlatformAdmin,
    node_id: int,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    return success(service.list_checks(center, node_id, limit=limit))


@router.get("/nodes/{node_id}/probe-latency", summary="节点公网探测耗时历史（按 target）")
def node_probe_latency(
    center: CenterDb,
    _: PlatformAdmin,
    node_id: int,
    hours: int = Query(default=24, ge=1, le=720, description="回看小时数"),
) -> dict:
    """公网探测耗时（root/healthz/api 分开统计）与业务请求耗时分离展示。"""
    from app.modules.ops import performance

    return success(performance.probe_latency_overview(center, node_id, hours=hours))


@router.get("/metrics/http", summary="管理 API 请求性能（请求量 / 5xx / P95 / P99）")
def http_metrics(
    _: PlatformAdmin,
    window_seconds: int = Query(
        default=900, ge=60, le=86400, description="统计窗口（秒），默认 15 分钟"
    ),
    route: str = Query(default="", max_length=200, description="按路由模板过滤"),
    method: str = Query(default="", max_length=16, description="按 HTTP 方法过滤"),
) -> dict:
    """业务请求性能指标（P95/P99 由固定直方图桶计算，Redis 不可用时 degraded）。"""
    from app.modules.ops import performance

    return success(
        performance.http_metrics(
            window_seconds=window_seconds, route=route or None, method=method or None
        )
    )


@router.get("/releases", summary="发布版本列表")
def releases(center: CenterDb, _: PlatformAdmin) -> dict:
    return success(service.list_releases(center))


@router.post("/releases", summary="登记发布版本")
def create_release(center: CenterDb, admin: PlatformAdmin, payload: ReleaseCreate) -> dict:
    return success(service.create_release(center, payload, actor_user_id=admin.user_id))


@router.patch("/releases/{release_id}", summary="更新发布版本（回填 commit/构建信息）")
def update_release(
    center: CenterDb, admin: PlatformAdmin, release_id: int, payload: ReleaseUpdate
) -> dict:
    return success(
        service.update_release(center, release_id, payload, actor_user_id=admin.user_id)
    )


@router.get("/operations", summary="运维任务列表")
def operations(center: CenterDb, _: PlatformAdmin, pagination: PageParams = Depends(), instance_id: str = Query(default="", max_length=32)) -> dict:
    data = service.list_operations(center, instance_id=int(instance_id) if instance_id.isdigit() else None, page=pagination.page, page_size=pagination.page_size)
    return paged(data["items"], data["total"], data["page"], data["page_size"])


@router.post("/instances/{instance_id}/operations", summary="创建运维任务")
def create_operation(center: CenterDb, admin: PlatformAdmin, instance_id: int, payload: OperationCreate) -> dict:
    return success(service.create_operation(center, instance_id, payload, actor_user_id=admin.user_id))


@router.get("/operations/{job_id}", summary="运维任务详情")
def operation_detail(center: CenterDb, _: PlatformAdmin, job_id: int) -> dict:
    return success(service.get_operation(center, job_id))


@router.post("/operations/{job_id}/retry", summary="重试运维任务")
def retry_operation(center: CenterDb, admin: PlatformAdmin, job_id: int) -> dict:
    return success(service.retry_operation(center, job_id, actor_user_id=admin.user_id))


@router.get("/backups", summary="备份记录列表")
def backups(center: CenterDb, _: PlatformAdmin, pagination: PageParams = Depends(), instance_id: str = Query(default="", max_length=32)) -> dict:
    data = service.list_backups(center, instance_id=int(instance_id) if instance_id.isdigit() else None, page=pagination.page, page_size=pagination.page_size)
    return paged(data["items"], data["total"], data["page"], data["page_size"])


@router.get("/backups/{backup_id}", summary="备份详情")
def backup_detail(center: CenterDb, _: PlatformAdmin, backup_id: int) -> dict:
    return success(service.get_backup(center, backup_id))


@router.post("/backups/{backup_id}/restore-check", summary="记录隔离环境恢复验证结果")
def backup_restore_check(
    center: CenterDb, admin: PlatformAdmin, backup_id: int, payload: BackupRestoreCheckIn
) -> dict:
    return success(
        service.record_restore_check(
            center,
            backup_id,
            status=payload.status,
            notes=payload.notes,
            actor_user_id=admin.user_id,
        )
    )


@router.get("/instances/{instance_id}/maintenance-windows", summary="实例维护窗口")
def maintenance_windows(center: CenterDb, _: PlatformAdmin, instance_id: int) -> dict:
    return success(service.list_maintenance_windows(center, instance_id))


@router.post("/instances/{instance_id}/maintenance-windows", summary="创建维护窗口")
def create_maintenance_window(center: CenterDb, admin: PlatformAdmin, instance_id: int, payload: MaintenanceWindowCreate) -> dict:
    return success(service.create_maintenance_window(center, instance_id, payload, actor_user_id=admin.user_id))


@router.delete("/maintenance-windows/{window_id}", summary="删除维护窗口")
def delete_maintenance_window(center: CenterDb, _: PlatformAdmin, window_id: int) -> dict:
    return success(service.delete_maintenance_window(center, window_id))


@router.get("/alerts", summary="告警列表")
def list_alerts(
    center: CenterDb,
    _: PlatformAdmin,
    pagination: PageParams = Depends(),
    status: str = Query(default="", max_length=16),
    node_id: str = Query(default="", max_length=32),
) -> dict:
    node_id_value = int(node_id) if node_id.isdigit() else None
    data = service.list_alerts(
        center,
        status=status,
        node_id=node_id_value,
        page=pagination.page,
        page_size=pagination.page_size,
    )
    return paged(data["items"], data["total"], data["page"], data["page_size"])


@router.post("/alerts/{alert_id}/ack", summary="确认告警")
def acknowledge_alert(
    center: CenterDb, admin: PlatformAdmin, alert_id: int, payload: AlertAckIn
) -> dict:
    return success(
        service.acknowledge_alert(
            center, alert_id, note=payload.note, actor_user_id=admin.user_id
        )
    )


@router.websocket("/nodes/{node_id}/ssh/ws")
async def node_ssh_websocket(websocket: WebSocket, node_id: int) -> None:
    """浏览器交互式 SSH 终端：Cookie 鉴权 + 30s 会话复核 + 15min 空闲断开。

    * 阻塞的 Redis/DB/Paramiko 调用全部放线程池，不阻塞事件循环；
    * 连接前校验主机指纹（不匹配关闭码 4409），连接/终端打开/关闭写平台审计；
    * 每 30s 复核会话、账号状态与平台管理员权限，撤权/注销关闭连接（4411）；
    * 15 分钟无输入自动断开（4410）。
    """
    import asyncio
    import json
    import threading
    import time

    from starlette.concurrency import run_in_threadpool

    from app.core import auth as auth_core
    from app.core import redis as redis_core
    from app.core.config import settings
    from app.core.context import new_request_id

    request_id = new_request_id()
    origin = websocket.headers.get("origin", "")
    allowed = auth_core.allowed_origins()
    if allowed and origin not in allowed:
        await websocket.close(code=4403)
        return
    session_id = websocket.cookies.get(settings.session_cookie_name, "")
    payload = (
        await run_in_threadpool(redis_core.read_session, session_id) if session_id else None
    )
    if not payload or auth_core.session_scope(payload) not in (None, "platform"):
        await websocket.close(code=4401)
        return
    user_id = int(payload.get("user_id") or 0)
    user = (
        await run_in_threadpool(service.load_platform_ssh_user, user_id) if user_id else None
    )
    if user is None or user.status != "active" or not user.is_platform_admin:
        await websocket.close(code=4403)
        return
    ip = websocket.client.host if websocket.client is not None else ""
    try:
        await run_in_threadpool(service.ssh_prepare_terminal, node_id)
    except AppError:
        await websocket.close(code=4404)
        return
    except Exception:
        logger.warning("SSH WS 目标不可用 node=%s", node_id, exc_info=True)
        await websocket.close(code=4404)
        return

    await websocket.accept()
    resources: dict[str, Any] | None = None
    reason = "client_closed"
    started = time.monotonic()
    input_bytes = 0
    try:
        try:
            resources = await run_in_threadpool(
                service.ssh_open_terminal,
                node_id,
                actor_user_id=user_id,
                request_id=request_id,
                ip=ip,
            )
        except AppError as exc:
            details = exc.details if isinstance(exc.details, dict) else {}
            code = 4409 if details.get("reason") == "fingerprint_mismatch" else 1011
            await websocket.send_text(f"\r\n{exc.message}\r\n")
            await websocket.close(code=code)
            return
        except Exception as exc:  # noqa: BLE001 - 连接异常反馈给终端页面
            logger.exception("SSH WS 连接失败 node=%s", node_id)
            await websocket.send_text(f"\r\nSSH 连接失败：{str(exc)[:300]}\r\n")
            await websocket.close(code=1011)
            return

        channel = resources["channel"]
        loop = asyncio.get_running_loop()

        def reader() -> None:
            while True:
                try:
                    data = channel.recv(4096)
                except TimeoutError:
                    continue
                except Exception:
                    break
                if not data:
                    break
                asyncio.run_coroutine_threadsafe(
                    websocket.send_text(data.decode("utf-8", "replace")), loop
                )
            asyncio.run_coroutine_threadsafe(websocket.close(), loop)

        threading.Thread(target=reader, daemon=True, name=f"ssh-reader-{node_id}").start()
        last_activity = time.monotonic()
        last_check = last_activity
        while True:
            now = time.monotonic()
            if now - last_activity >= service.SSH_WS_IDLE_SECONDS:
                reason = "idle_timeout"
                try:
                    await websocket.send_text("\r\n空闲超过 15 分钟，SSH 连接已断开。\r\n")
                    await websocket.close(code=4410)
                except Exception:  # pragma: no cover - 对端已断开
                    pass
                break
            if now - last_check >= service.SSH_SESSION_RECHECK_SECONDS:
                last_check = now
                ok, check_reason = await run_in_threadpool(
                    service.ssh_session_recheck, session_id, user_id
                )
                if not ok:
                    reason = check_reason or "session_revoked"
                    try:
                        await websocket.send_text("\r\n会话或平台权限已失效，SSH 连接已断开。\r\n")
                        await websocket.close(code=4411)
                    except Exception:  # pragma: no cover - 对端已断开
                        pass
                    break
            wait_seconds = max(
                0.1,
                min(
                    service.SSH_SESSION_RECHECK_SECONDS - (time.monotonic() - last_check),
                    service.SSH_WS_IDLE_SECONDS - (time.monotonic() - last_activity),
                ),
            )
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                continue
            last_activity = time.monotonic()
            input_bytes += len(raw)
            try:
                message = json.loads(raw)
            except ValueError:
                await run_in_threadpool(channel.send, raw)
                continue
            if message.get("type") == "input":
                data = str(message.get("data") or "")
                input_bytes += len(data)
                await run_in_threadpool(channel.send, data)
            elif message.get("type") == "resize":
                await run_in_threadpool(
                    channel.resize_pty,
                    width=int(message.get("cols") or 120),
                    height=int(message.get("rows") or 32),
                )
    except WebSocketDisconnect:
        reason = "client_disconnected"
    except Exception:  # noqa: BLE001 - 终端异常不能影响其它连接
        logger.exception("SSH WS 会话异常 node=%s", node_id)
        reason = "error"
    finally:
        duration = int(time.monotonic() - started)
        if resources is not None:
            try:
                await run_in_threadpool(
                    service.ssh_close_terminal,
                    resources,
                    actor_user_id=user_id,
                    request_id=request_id,
                    ip=ip,
                    reason=reason,
                    duration_seconds=duration,
                    input_bytes=input_bytes,
                )
            except Exception:  # pragma: no cover - 关闭审计失败不影响资源释放
                logger.exception("SSH WS 关闭审计失败 node=%s", node_id)
