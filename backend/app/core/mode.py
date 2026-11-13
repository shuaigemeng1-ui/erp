"""部署形态边界与固定实例租户绑定（纯函数）。

批次2「实例隔离与租户模式边界」：

* ``deployment_mode=center``：统一管理中心，平台 / Agent / 租户业务接口全部可用；
* ``deployment_mode=tenant``：租户独立实例，只服务配置的固定租户，
  拒绝 ``/api/v1/platform/*``、``/api/v1/agent/*``（含平台 SSH WebSocket），
  返回统一 404，不暴露平台功能存在；健康检查与租户业务接口正常；
* ``deployment_mode=gateway``：独立集成网关，仅网关接口与健康检查（由 main 中间件处理）。

固定实例绑定：tenant 模式下 ``settings.tenant_id`` 是本实例唯一服务的租户。
HTTP 路径参数 / 查询参数 / JSON 请求体里出现的 ``tenant_id`` 必须与之一致，
否则 403；Worker / Celery 任务 payload 使用 :func:`ensure_task_payload_tenant_id` 校验。

本模块不依赖数据库，可被中间件、依赖、Worker 直接复用。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode

# 平台 / Agent 专属路径（tenant 模式一律拒绝；不含租户业务与认证接口）
PLATFORM_PATH_SUFFIXES = ("/platform", "/agent")
DEPLOYMENT_MODES = ("center", "tenant", "gateway")


def _api_base() -> str:
    return (settings.api_prefix or "/api/v1").rstrip("/")


def platform_only_path(path: str) -> bool:
    """是否平台 / Agent 专属路径：``/api/v1/platform...`` 或 ``/api/v1/agent...``。"""
    normalized = path or "/"
    base = _api_base()
    for suffix in PLATFORM_PATH_SUFFIXES:
        prefix = f"{base}{suffix}"
        if normalized == prefix or normalized.startswith(f"{prefix}/"):
            return True
    return False


def mode_allows_path(mode: str, path: str) -> bool:
    """当前部署形态是否允许访问该路径（健康检查、认证与业务接口始终放行）。"""
    if mode == "center":
        return True
    if mode == "tenant":
        return not platform_only_path(path)
    if mode == "gateway":
        if path in {"/healthz", "/livez", "/readyz"}:
            return True
        return path.startswith(f"{_api_base()}/gateway")
    return False


def boundary_violation(mode: str, path: str) -> bool:
    """路径是否被当前部署形态拒绝（用于中间件，返回统一 404）。"""
    return not mode_allows_path(mode, path)


def fixed_tenant_id() -> int:
    """本实例固定绑定的租户ID；0 表示未配置（center 模式或尚未配置）。"""
    return int(getattr(settings, "tenant_id", 0) or 0)


def fixed_instance_id() -> int:
    return int(getattr(settings, "instance_id", 0) or 0)


def tenant_binding_enabled() -> bool:
    return settings.deployment_mode == "tenant"


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def collect_tenant_ids(payload: Any, *, depth: int = 0) -> list[int]:
    """递归收集 payload 中所有 ``tenant_id`` 值（dict / list，限制深度防脏数据）。"""
    if depth > 6:
        return []
    found: list[int] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "tenant_id":
                coerced = _coerce_int(value)
                if coerced is not None:
                    found.append(coerced)
                continue
            found.extend(collect_tenant_ids(value, depth=depth + 1))
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found.extend(collect_tenant_ids(item, depth=depth + 1))
    return found


def ensure_tenant_id_matches(tenant_id: int | None, *, expected: int | None = None) -> None:
    """校验单个 tenant_id 与实例固定绑定一致；不一致抛 403。"""
    if not tenant_binding_enabled():
        return
    bound = fixed_tenant_id() if expected is None else int(expected)
    if tenant_id is None:
        return
    if bound <= 0:
        raise AppError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "当前实例未配置 TENANT_ID，无法处理租户请求",
            details={"configured": False},
        )
    if int(tenant_id) != bound:
        raise AppError(
            ErrorCode.TENANT_FORBIDDEN,
            "请求租户与当前实例绑定不一致",
            details={"bound_tenant_id": str(bound), "request_tenant_id": str(tenant_id)},
        )


def ensure_payload_tenant_id(payload: Any) -> None:
    """校验请求体 / 任务 payload 中所有 tenant_id 与实例绑定一致。"""
    for tenant_id in collect_tenant_ids(payload):
        ensure_tenant_id_matches(tenant_id)


def ensure_task_payload_tenant_id(payload: Any, *, expected_tenant_id: int) -> None:
    """Worker / Celery 任务 payload 校验：payload 中 tenant_id 必须等于任务库租户。"""
    bound = int(expected_tenant_id or 0)
    for tenant_id in collect_tenant_ids(payload):
        if bound <= 0 or tenant_id != bound:
            raise AppError(
                ErrorCode.TENANT_FORBIDDEN,
                "任务 payload 的 tenant_id 与任务所属租户不一致",
                details={"expected_tenant_id": str(bound), "payload_tenant_id": str(tenant_id)},
            )


def instance_binding_summary() -> dict[str, Any]:
    """运维自检 / ``/api/v1/runtime`` 展示的实例绑定摘要（不含敏感值）。"""
    return {
        "mode": settings.deployment_mode,
        "tenant_id": str(fixed_tenant_id()) if fixed_tenant_id() else None,
        "instance_id": str(fixed_instance_id()) if fixed_instance_id() else None,
    }


__all__ = [
    "DEPLOYMENT_MODES",
    "boundary_violation",
    "collect_tenant_ids",
    "ensure_payload_tenant_id",
    "ensure_task_payload_tenant_id",
    "ensure_tenant_id_matches",
    "fixed_instance_id",
    "fixed_tenant_id",
    "instance_binding_summary",
    "mode_allows_path",
    "platform_only_path",
    "tenant_binding_enabled",
]
