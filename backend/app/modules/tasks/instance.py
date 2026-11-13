"""后台任务的固定实例 / 租户绑定（Worker、Scheduler、Outbox 共用）。

背景（批次2：实例隔离）：

* 独立部署（``deployment_mode=tenant``）的实例只服务自己的租户；
* 中心聚合 Worker 可以被绑定到某个实例（``instance_id``），只处理该实例下的租户；
* 任务消息（Celery payload / 数据库 outbox payload）里的 ``tenant_id``、``instance_id``
  **不可信**：绑定只从进程环境读取，事件以所在租户库的 ``outbox_events.tenant_id`` 为准；
* 租户必须处于 active；已独立部署 / 迁移中的租户不再由旧中心 Worker/Scheduler 调度；
* 任何拒绝都写审计（能打开租户库时），并保留清晰原因。

环境变量（部署时固定，不随任务变化）::

    ERP_WORKER_INSTANCE_ID=12          # 本进程服务的实例ID（可选）
    ERP_WORKER_TENANT_ID=3             # 独立部署时本进程唯一服务的租户ID（tenant 模式必填）
    ERP_WORKER_STRICT_BINDING=true     # 缺省：tenant 模式为 true，center 模式为 false
    DEPLOYMENT_MODE=center             # 与 app.core.config.settings.deployment_mode 一致
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Iterator

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.db.session import center_session, tenant_session

logger = logging.getLogger("ventasync.tasks.instance")

ENV_INSTANCE_ID = "ERP_WORKER_INSTANCE_ID"
ENV_TENANT_ID = "ERP_WORKER_TENANT_ID"
ENV_STRICT = "ERP_WORKER_STRICT_BINDING"
# 兼容运维惯用写法（不覆盖 ERP_* 优先级）
ALT_ENV_INSTANCE_ID = ("WORKER_INSTANCE_ID", "INSTANCE_ID")
ALT_ENV_TENANT_ID = ("WORKER_TENANT_ID", "TENANT_ID")

DEPLOYMENT_MODES = ("center", "tenant", "gateway")
INSTANCE_MODES = ("shared", "independent", "unknown")
# 迁移生命周期中不再参与旧中心调度的状态（迁移中 / 已迁出 / 回滚过程中）
BLOCKED_MIGRATION_STATUSES = frozenset({"migrating", "migrated", "rollback"})
# 迁移状态中允许旧中心调度的值（仅登记）
ALLOWED_MIGRATION_STATUSES = frozenset({"registered"})
# 独立部署的实例不参与共享中心调度
BLOCKED_INSTANCE_MODES = frozenset({"independent"})

# 任务上下文中已校验通过的租户（校验一次即可，避免每个子动作重复查中心库）
_task_tenant_var: ContextVar[int | None] = ContextVar("task_tenant_id", default=None)
_binding_var: ContextVar["WorkerBinding | None"] = ContextVar("worker_binding", default=None)


class BindingConfigError(RuntimeError):
    """工作进程绑定配置错误（启动期应 fail fast，而不是静默串租户）。"""


@dataclass(frozen=True, slots=True)
class WorkerBinding:
    """本进程固定的实例 / 租户绑定（来自环境变量，任务消息不能覆盖）。"""

    instance_id: int | None
    tenant_id: int | None
    deployment_mode: str
    strict: bool

    def describe(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "tenant_id": self.tenant_id,
            "deployment_mode": self.deployment_mode,
            "strict": self.strict,
        }

    @property
    def bound(self) -> bool:
        return self.instance_id is not None or self.tenant_id is not None


@dataclass(frozen=True, slots=True)
class TenantDeployment:
    """中心库读取到的租户部署信息（字段缺失时按旧版兼容为 unknown）。"""

    tenant_id: int
    tenant_status: str = "unknown"
    instance_id: int | None = None
    deployment_mode: str = "unknown"
    migration_status: str = "unknown"
    source: str = "legacy"

    @property
    def exists(self) -> bool:
        return self.tenant_status != "unknown"


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    """是否允许本进程调度 / 执行某租户的任务。"""

    allowed: bool
    reason: str = ""
    tenant_status: str = ""
    instance_id: int | None = None
    deployment_mode: str = "unknown"
    migration_status: str = "unknown"
    audit_required: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "tenant_status": self.tenant_status,
            "instance_id": self.instance_id,
            "deployment_mode": self.deployment_mode,
            "migration_status": self.migration_status,
        }


def _positive_int(value: Any) -> int | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise BindingConfigError(f"绑定环境变量必须是正整数，实际为 {value!r}") from exc
    if parsed <= 0:
        raise BindingConfigError(f"绑定环境变量必须是正整数，实际为 {value!r}")
    return parsed


def _first_env(env: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = env.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return None


def _truthy(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise BindingConfigError(f"布尔型绑定环境变量取值非法：{value!r}")


def load_worker_binding(
    env: Mapping[str, str] | None = None,
    *,
    deployment_mode: str | None = None,
) -> WorkerBinding:
    """从进程环境解析固定绑定；非法配置直接抛错（启动期 fail fast）。"""
    source = os.environ if env is None else env
    mode = str(deployment_mode or source.get("DEPLOYMENT_MODE") or settings.deployment_mode or "center")
    if mode not in DEPLOYMENT_MODES:
        raise BindingConfigError(f"未知 deployment_mode：{mode!r}")
    instance_raw = _first_env(source, (ENV_INSTANCE_ID, *ALT_ENV_INSTANCE_ID))
    tenant_raw = _first_env(source, (ENV_TENANT_ID, *ALT_ENV_TENANT_ID))
    instance_id = _positive_int(instance_raw)
    tenant_id = _positive_int(tenant_raw)
    strict_raw = _truthy(_first_env(source, (ENV_STRICT,)))
    strict = bool(strict_raw) if strict_raw is not None else mode == "tenant"
    if strict and tenant_id is None:
        raise BindingConfigError(
            f"严格绑定模式（{mode}）必须设置 {ENV_TENANT_ID}，否则可能处理到其它租户的任务"
        )
    if mode == "tenant" and tenant_id is None:
        raise BindingConfigError("租户独立部署进程必须设置 ERP_WORKER_TENANT_ID")
    return WorkerBinding(
        instance_id=instance_id,
        tenant_id=tenant_id,
        deployment_mode=mode,
        strict=strict,
    )


def current_binding() -> WorkerBinding:
    """当前进程绑定（ContextVar 优先，首次使用时从环境加载）。"""
    existing = _binding_var.get()
    if existing is not None:
        return existing
    binding = load_worker_binding()
    _binding_var.set(binding)
    return binding


def reset_binding_cache() -> None:
    """测试 / 配置热更新时清空进程级绑定缓存。"""
    _binding_var.set(None)


def set_task_tenant(tenant_id: int | None) -> Any:
    return _task_tenant_var.set(int(tenant_id) if tenant_id else None)


def reset_task_tenant(token: Any) -> None:
    _task_tenant_var.reset(token)


def task_context_tenant() -> int | None:
    """当前任务上下文里已校验通过的租户ID；None 表示尚未校验。"""
    return _task_tenant_var.get()


@contextmanager
def task_tenant_scope(tenant_id: int) -> Iterator[int]:
    """标记「本任务已通过绑定校验」，供处理器内部快速复核。"""
    token = set_task_tenant(tenant_id)
    try:
        yield int(tenant_id)
    finally:
        reset_task_tenant(token)


# --------------------------------------------------------------------------- 中心库读取
def _safe_center_query(center: Session, sql: str, params: dict[str, Any]) -> Any:
    try:
        return center.execute(text(sql), params).first()
    except SQLAlchemyError:
        return None


def load_tenant_deployment(center: Session, tenant_id: int) -> TenantDeployment:
    """读取租户状态与实例部署信息；新字段（T23）未上线时自动回退。

    只读查询：不改 identity/platform 模型，也不假设 ``tenant_instances`` 新列一定存在。
    """
    tenant_row = _safe_center_query(
        center,
        "SELECT id, status FROM tenants WHERE id = :tenant_id",
        {"tenant_id": int(tenant_id)},
    )
    if tenant_row is None:
        return TenantDeployment(tenant_id=int(tenant_id), source="center")
    tenant_status = str(tenant_row[1] or "")
    instance_row = _safe_center_query(
        center,
        "SELECT id, deployment_mode, migration_status FROM tenant_instances"
        " WHERE tenant_id = :tenant_id",
        {"tenant_id": int(tenant_id)},
    )
    if instance_row is None:
        # 兼容旧版 center 库：还没有 deployment_mode / migration_status 两列
        legacy = _safe_center_query(
            center,
            "SELECT id FROM tenant_instances WHERE tenant_id = :tenant_id",
            {"tenant_id": int(tenant_id)},
        )
        return TenantDeployment(
            tenant_id=int(tenant_id),
            tenant_status=tenant_status,
            instance_id=int(legacy[0]) if legacy is not None and legacy[0] is not None else None,
            deployment_mode="unknown",
            migration_status="unknown",
            source="center-legacy",
        )
    return TenantDeployment(
        tenant_id=int(tenant_id),
        tenant_status=tenant_status,
        instance_id=int(instance_row[0]) if instance_row[0] is not None else None,
        deployment_mode=str(instance_row[1] or "unknown"),
        migration_status=str(instance_row[2] or "unknown"),
        source="center",
    )


def evaluate_dispatch(binding: WorkerBinding, deployment: TenantDeployment) -> DispatchDecision:
    """核心判定（纯函数）：绑定、租户状态、部署模式、迁移状态。"""
    base = DispatchDecision(
        allowed=False,
        tenant_status=deployment.tenant_status,
        instance_id=deployment.instance_id,
        deployment_mode=deployment.deployment_mode,
        migration_status=deployment.migration_status,
    )
    if not deployment.exists:
        return replace(base, reason="tenant_not_found", audit_required=True)
    if deployment.tenant_status != "active":
        return replace(base, reason="tenant_not_active", audit_required=True)
    if binding.tenant_id is not None and int(binding.tenant_id) != int(deployment.tenant_id):
        return replace(base, reason="tenant_binding_mismatch", audit_required=True)
    if binding.instance_id is not None:
        if deployment.instance_id is not None and int(deployment.instance_id) != int(binding.instance_id):
            return replace(base, reason="instance_binding_mismatch", audit_required=True)
        if deployment.instance_id is None and binding.strict:
            return replace(base, reason="tenant_instance_unbound", audit_required=True)
    if deployment.deployment_mode in BLOCKED_INSTANCE_MODES:
        return replace(base, reason="tenant_independent_deployment", audit_required=False)
    if deployment.migration_status in BLOCKED_MIGRATION_STATUSES:
        return replace(base, reason="tenant_migrating", audit_required=False)
    return replace(base, allowed=True, reason="")


def deployment_state_of(
    tenant_id: int, *, center: Session | None = None
) -> TenantDeployment:
    """读取租户部署状态（可复用外部中心库 Session）。"""
    if center is not None:
        return load_tenant_deployment(center, tenant_id)
    with center_session() as session:
        return load_tenant_deployment(session, tenant_id)


def evaluate_current_dispatch(
    tenant_id: int, *, center: Session | None = None, binding: WorkerBinding | None = None
) -> DispatchDecision:
    """用当前进程绑定判定某租户；供 Worker / Scheduler / Outbox 统一调用。"""
    binding = binding or current_binding()
    return evaluate_dispatch(binding, deployment_state_of(tenant_id, center=center))


def audit_rejection(
    tenant_id: int,
    *,
    reason: str,
    action: str = "task.binding_rejected",
    summary: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """拒绝调度 / 执行时写审计（尽力而为，不影响主流程）。

    直接按中心库路由打开租户库，**不走** ``job_tenant_session``（避免与校验逻辑递归）。
    """
    payload = {
        "reason": reason,
        **(extra or {}),
    }
    try:
        from app.modules.identity.service import load_tenant_db_route

        with center_session() as center:
            route = load_tenant_db_route(center, tenant_id)
        if route is None:
            return
        from app.modules.audit.service import record

        with tenant_session(route) as db:
            record(
                db,
                tenant_id=int(tenant_id),
                action=action,
                resource_type="task",
                resource_id=None,
                actor_id=None,
                summary=(summary or "后台任务实例绑定校验拒绝")[:500],
                payload=payload,
            )
            db.commit()
    except Exception:  # noqa: BLE001 - 审计失败只记录日志
        logger.warning("写实例绑定拒绝审计失败 tenant=%s reason=%s", tenant_id, reason)


def ensure_dispatch_allowed(
    tenant_id: int, *, purpose: str = "task", center: Session | None = None
) -> DispatchDecision:
    """强制执行校验：拒绝时写审计并抛 ``AppError``（永久错误，不重试）。"""
    decision = evaluate_current_dispatch(tenant_id, center=center)
    if decision.allowed:
        return decision
    logger.warning(
        "拒绝调度租户任务 tenant=%s purpose=%s reason=%s binding=%s",
        tenant_id,
        purpose,
        decision.reason,
        current_binding().describe(),
    )
    if decision.audit_required:
        audit_rejection(
            tenant_id,
            reason=decision.reason,
            summary=f"后台任务被拒绝（{purpose}）：{decision.reason}",
            extra={"purpose": purpose, **decision.describe()},
        )
    raise AppError(
        ErrorCode.TENANT_FORBIDDEN,
        f"租户不满足本实例调度条件：{decision.reason}",
        details={"reason": decision.reason, "purpose": purpose},
    )


def filter_dispatchable_tenants(
    tenant_ids: list[int],
    *,
    binding: WorkerBinding | None = None,
    loader: Any | None = None,
) -> tuple[list[int], list[dict[str, Any]]]:
    """批量过滤可调度租户，返回 ``(允许的租户, 被跳过的明细)``。

    ``loader`` 可注入（测试用），默认从中心库读取部署状态。
    """
    binding = binding or current_binding()
    load = loader or deployment_state_of
    allowed: list[int] = []
    skipped: list[dict[str, Any]] = []
    for tenant_id in tenant_ids:
        try:
            deployment = load(int(tenant_id))
        except Exception as exc:  # noqa: BLE001 - 单个租户读取失败按跳过处理
            skipped.append({"tenant_id": int(tenant_id), "reason": f"load_failed:{exc}"})
            continue
        decision = evaluate_dispatch(binding, deployment)
        if decision.allowed:
            allowed.append(int(tenant_id))
        else:
            skipped.append({"tenant_id": int(tenant_id), **decision.describe()})
    return allowed, skipped


# --------------------------------------------------------------------------- 任务消息校验
def payload_binding_conflict(
    payload: Mapping[str, Any] | None,
    *,
    tenant_id: int,
    binding: WorkerBinding | None = None,
) -> str | None:
    """检查任务 payload 是否试图覆盖租户 / 实例；返回冲突原因，None 表示无冲突。

    payload 里的 ``tenant_id`` / ``instance_id`` 只允许「与权威值一致」或缺失；
    不一致时拒绝投递（防止伪造消息串租户）。权威值来自 outbox 行的 tenant_id 与进程绑定。
    """
    if not payload:
        return None
    binding = binding or current_binding()
    raw_tenant = payload.get("tenant_id")
    if raw_tenant is not None:
        try:
            payload_tenant = int(raw_tenant)
        except (TypeError, ValueError):
            return "payload_tenant_invalid"
        if payload_tenant != int(tenant_id):
            return "payload_tenant_mismatch"
    raw_instance = payload.get("instance_id")
    if raw_instance is not None:
        try:
            payload_instance = int(raw_instance)
        except (TypeError, ValueError):
            return "payload_instance_invalid"
        if binding.instance_id is None:
            return "payload_instance_not_allowed"
        if payload_instance != int(binding.instance_id):
            return "payload_instance_mismatch"
    return None


def authoritative_tenant(payload: Mapping[str, Any] | None, event_tenant_id: int) -> int:
    """事件 / 任务消息的权威租户：数据库行 > payload（payload 仅作一致性校验）。"""
    del payload  # 明确不使用 payload 作为来源
    return int(event_tenant_id)


__all__ = [
    "ALLOWED_MIGRATION_STATUSES",
    "BLOCKED_INSTANCE_MODES",
    "BLOCKED_MIGRATION_STATUSES",
    "BindingConfigError",
    "DispatchDecision",
    "ENV_INSTANCE_ID",
    "ENV_STRICT",
    "ENV_TENANT_ID",
    "TenantDeployment",
    "WorkerBinding",
    "audit_rejection",
    "authoritative_tenant",
    "current_binding",
    "deployment_state_of",
    "ensure_dispatch_allowed",
    "evaluate_current_dispatch",
    "evaluate_dispatch",
    "filter_dispatchable_tenants",
    "load_tenant_deployment",
    "load_worker_binding",
    "payload_binding_conflict",
    "reset_binding_cache",
    "reset_task_tenant",
    "set_task_tenant",
    "task_context_tenant",
    "task_tenant_scope",
]
