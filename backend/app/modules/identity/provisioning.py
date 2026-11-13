"""租户开通流水线与平台初始化。

流水线（见 plan.txt §2「租户生命周期」，顺序固定）：

```text
中心库登记租户
→ 创建业务库与受限账号（运维身份）
→ 执行租户库 Alembic 迁移（独立子进程）
→ 初始化租户设置、预置角色、默认库存地点
→ 绑定套餐与订阅
→ 发出负责人邀请
→ 标记 active
```

约定：

* 每一步写一行 ``provisioning_jobs``（pending → running → succeeded / failed），
  已成功的步骤重试时直接跳过，保证开通可重试且幂等；
* 任一步失败：当前步骤记 ``failed`` + 失败原因，租户置 ``provision_failed`` 且**不激活**；
* 建库建号使用 ``provisioner_engine``（运维身份），业务运行账号只授予本库 DML 权限；
* 迁移通过 ``python -m alembic -c alembic/tenant.ini -x db_url=... upgrade head`` 独立执行，
  不在 API 启动时自动迁移；
* 邀请令牌只存 SHA-256 摘要，原文只在本次调用结果中返回一次。

``bootstrap_platform_admin`` 与 ``provision_tenant`` 供 ``app/cli.py`` 与平台路由调用。
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.permissions import PlatformRole
from app.core.security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    random_token,
    sha256_hex,
    validate_password_strength,
)
from app.db.base import utcnow
from app.db.session import provisioner_engine, tenant_engines, tenant_session
from app.modules.access.service import ensure_tenant_settings
from app.modules.identity import repository as repo
from app.modules.identity.constants import (
    DEFAULT_STOCK_PLACE_CODE,
    DEFAULT_STOCK_PLACE_NAME,
    InvitationStatus,
    MembershipStatus,
    PROVISIONING_STEP_LABELS,
    PROVISIONING_STEPS,
    ProvisioningStatus,
    ProvisioningStep,
    TENANT_DATABASE_READY,
    TenantStatus,
)
from app.modules.identity.models_center import (
    PlatformRole as PlatformRoleModel,
    PlatformUserRole,
    ProvisioningJob,
    Tenant,
    TenantDatabase,
    TenantInvitation,
    TenantMembership,
    User,
)
from app.modules.identity.service import (
    RoleBindingChoice,
    ScopeChoice,
    run_in_transaction,
    seed_predefined_roles,
)
from app.modules.platform.models_center import Plan, PlanFeature, Subscription

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[3]
_DB_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_TENANT_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
# 数据库 URL 中的账号密码（用于错误信息脱敏，避免把密码写入 provisioning_jobs）
_URL_CREDENTIALS_PATTERN = re.compile(r"([A-Za-z0-9+.\-]+://)([^:@/\s]+):([^@/\s]+)@")

# 默认套餐：平台管理员可用 PUT/PATCH 调整
DEFAULT_PLAN_CODE = "starter"
DEFAULT_PLAN_NAME = "标准版"
DEFAULT_PLAN_FEATURES: dict[str, int] = {
    "max_members": 10,
    "max_stores": 2,
    "max_skus": 2000,
}
DEFAULT_FEATURE_CODES: tuple[str, ...] = ("order_sync", "stock_push", "channel_listing")

# Celery 任务名（与 app/modules/identity/tasks.py 保持一致，Lead 加入 worker include）
PROVISIONING_TASK_NAME = "app.modules.identity.tasks.provision_tenant_job"
PROVISIONING_RETRY_TASK_NAME = "app.modules.identity.tasks.retry_provision_job"
PROVISIONING_QUEUE = "default"


# --------------------------------------------------------------------------- 结果结构
@dataclass
class ProvisioningResult:
    """一次开通 / 重试的结果，供平台路由与 CLI 使用。"""

    tenant_id: int
    status: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    failed_step: str = ""
    error_message: str = ""
    owner_invitation_token: str = ""
    finished: bool = False

    @property
    def succeeded(self) -> bool:
        return self.finished and not self.failed_step

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": str(self.tenant_id),
            "status": self.status,
            "steps": self.steps,
            "failed_step": self.failed_step,
            "error_message": self.error_message,
            "owner_invitation_token": self.owner_invitation_token,
        }


@dataclass
class _ProvisionContext:
    """流水线内部上下文（每次执行重新构造，避免跨请求共享状态）。"""

    center: Session
    tenant: Tenant
    owner_email: str = ""
    operator_user_id: int | None = None
    result: ProvisioningResult | None = None
    state: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- 平台初始化
def bootstrap_platform_admin(
    center: Session,
    *,
    email: str,
    password: str,
    display_name: str = "",
    locale: str = "zh-CN",
) -> User:
    """创建首个平台管理员（幂等，不开放公共注册）。

    已存在同邮箱账号时不重复创建、不覆盖既有密码，只补齐平台角色与默认套餐。
    """
    normalized = (email or "").strip().lower()
    if not normalized or "@" not in normalized:
        raise AppError(ErrorCode.VALIDATION_ERROR, "平台管理员邮箱格式不正确")
    validate_password_strength(password)
    with run_in_transaction(center):
        user = repo.get_user_by_email(center, normalized)
        if user is None:
            user = User(
                email=normalized,
                password_hash=hash_password(password),
                display_name=(display_name or "平台管理员")[:100],
                status="active",
                locale=locale or "zh-CN",
            )
            center.add(user)
            center.flush()
        _ensure_platform_roles(center)
        admin_role = repo.get_platform_role_by_code(center, PlatformRole.PLATFORM_ADMIN.value)
        if admin_role is None:
            raise AppError(ErrorCode.INTERNAL_ERROR, "平台角色初始化失败")
        _ensure_platform_user_role(center, int(user.id), int(admin_role.id))
        seed_default_plans(center)
        logger.info("平台管理员已就绪 user_id=%s", user.id)
        return user


def _ensure_platform_roles(center: Session) -> None:
    for code, name, description in (
        (PlatformRole.PLATFORM_ADMIN.value, "平台管理员", "平台全部管理权限"),
        (PlatformRole.PLATFORM_OPS.value, "平台运营", "平台运营只读与协助权限"),
    ):
        if repo.get_platform_role_by_code(center, code) is not None:
            continue
        center.add(
            PlatformRoleModel(code=code, name=name, description=description)
        )
        center.flush()


def _ensure_platform_user_role(center: Session, user_id: int, role_id: int) -> None:
    existing = center.execute(
        text("SELECT id FROM platform_user_roles WHERE user_id = :user_id AND role_id = :role_id"),
        {"user_id": user_id, "role_id": role_id},
    ).first()
    if existing is None:
        center.add(PlatformUserRole(user_id=user_id, role_id=role_id, granted_by=None))
        center.flush()


def seed_default_plans(center: Session) -> dict[str, Plan]:
    """写入默认套餐与额度（幂等）。"""
    plans: dict[str, Plan] = {}
    for code, name, is_default in (
        (DEFAULT_PLAN_CODE, DEFAULT_PLAN_NAME, True),
        ("trial", "试用版", False),
    ):
        plan = center.execute(select(Plan).where(Plan.code == code)).scalar_one_or_none()
        if plan is None:
            plan = Plan(
                code=code,
                name=name,
                status="active",
                description="平台默认套餐，可在平台管理中调整额度",
                is_default=is_default,
            )
            center.add(plan)
            center.flush()
            features = DEFAULT_PLAN_FEATURES.copy()
            if code == "trial":
                features = {"max_members": 3, "max_stores": 1, "max_skus": 200}
            for feature_code, limit_value in features.items():
                center.add(
                    PlanFeature(plan_id=int(plan.id), feature_code=feature_code, limit_value=limit_value)
                )
            center.flush()
        plans[code] = plan
    return plans


def ensure_default_plan(center: Session) -> Plan:
    """返回默认套餐；不存在时创建。"""
    plan = center.execute(
        select(Plan).where(Plan.is_default.is_(True)).order_by(Plan.id)
    ).scalars().first()
    if plan is None:
        plans = seed_default_plans(center)
        plan = plans[DEFAULT_PLAN_CODE]
    return plan


# --------------------------------------------------------------------------- 中心库登记
def create_tenant(
    center: Session,
    *,
    code: str,
    name: str,
    owner_email: str,
    timezone: str = "Asia/Shanghai",
    plan_id: int | None = None,
    operator_user_id: int | None = None,
) -> tuple[Tenant, list[ProvisioningJob]]:
    """中心库登记租户并创建开通任务行（不执行开通步骤）。"""
    normalized_code = (code or "").strip().lower()
    if not _TENANT_CODE_PATTERN.match(normalized_code):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "租户编码需为 2-40 位小写字母、数字、下划线或短横线，且以字母或数字开头",
        )
    normalized_email = (owner_email or "").strip().lower()
    if not normalized_email or "@" not in normalized_email:
        raise AppError(ErrorCode.VALIDATION_ERROR, "负责人邮箱格式不正确")
    if not (name or "").strip():
        raise AppError(ErrorCode.VALIDATION_ERROR, "租户名称不能为空")

    with run_in_transaction(center):
        if repo.get_tenant_by_code(center, normalized_code) is not None:
            raise AppError(ErrorCode.CONFLICT, "租户编码已存在")
        plan = None
        if plan_id is not None:
            plan = center.get(Plan, plan_id)
            if plan is None or plan.status != "active":
                raise AppError(ErrorCode.VALIDATION_ERROR, "套餐不存在或已停用")
        else:
            plan = ensure_default_plan(center)
        owner = _ensure_user(center, normalized_email)
        tenant = Tenant(
            code=normalized_code,
            name=name.strip()[:128],
            status=TenantStatus.PROVISIONING.value,
            timezone=timezone or "Asia/Shanghai",
            plan_id=int(plan.id) if plan is not None else None,
            owner_user_id=int(owner.id),
            suspended_at=None,
            suspended_reason=None,
        )
        center.add(tenant)
        center.flush()
        membership = _ensure_owner_membership(center, int(tenant.id), int(owner.id))
        jobs = [
            ProvisioningJob(
                tenant_id=int(tenant.id),
                step=step.value,
                status=ProvisioningStatus.PENDING.value,
                attempts=0,
            )
            for step in PROVISIONING_STEPS
        ]
        center.add_all(jobs)
        center.flush()
        logger.info(
            "租户已登记 tenant_id=%s code=%s owner_membership=%s operator=%s",
            tenant.id,
            tenant.code,
            membership.id,
            operator_user_id,
        )
        return tenant, jobs


def _ensure_user(center: Session, email: str) -> User:
    user = repo.get_user_by_email(center, email)
    if user is not None:
        return user
    user = User(
        email=email,
        password_hash=None,
        display_name=email.split("@")[0][:100],
        status="active",
        locale="zh-CN",
    )
    center.add(user)
    center.flush()
    return user


def _ensure_owner_membership(center: Session, tenant_id: int, user_id: int) -> TenantMembership:
    membership = repo.get_membership(center, tenant_id, user_id)
    if membership is not None:
        membership.is_owner = True
        if membership.status == MembershipStatus.DISABLED.value:
            membership.status = MembershipStatus.INVITED.value
        center.add(membership)
        center.flush()
        return membership
    membership = TenantMembership(
        tenant_id=tenant_id,
        user_id=user_id,
        member_no=repo.allocate_member_no(center, tenant_id),
        status=MembershipStatus.INVITED.value,
        is_owner=True,
    )
    center.add(membership)
    center.flush()
    return membership


# --------------------------------------------------------------------------- 流水线执行
def provision_tenant(center: Session, tenant_id: int, *, operator_user_id: int | None = None) -> ProvisioningResult:
    """执行（或重试）开通流水线；已成功的步骤跳过，失败即停止且不激活租户。"""
    tenant = repo.get_tenant(center, tenant_id)
    if tenant is None:
        raise AppError(ErrorCode.TENANT_NOT_FOUND)
    if tenant.status == TenantStatus.SUSPENDED.value:
        raise AppError(ErrorCode.UNSUPPORTED_ACTION, "租户已暂停，不能重试开通")
    if tenant.status == TenantStatus.ACTIVE.value:
        raise AppError(ErrorCode.UNSUPPORTED_ACTION, "租户已开通，无需重复执行")
    # 批次2：已独立部署 / 迁移中的实例不再由旧中心重新开通
    ensure_instance_allows_center_write(center, tenant_id, action="重新开通")

    owner_email = ""
    if tenant.owner_user_id:
        owner = repo.get_user(center, int(tenant.owner_user_id))
        owner_email = owner.email if owner else ""

    result = ProvisioningResult(tenant_id=int(tenant.id), status=tenant.status)
    context = _ProvisionContext(
        center=center,
        tenant=tenant,
        owner_email=owner_email,
        operator_user_id=operator_user_id,
        result=result,
    )

    with run_in_transaction(center):
        tenant.status = TenantStatus.PROVISIONING.value
        center.add(tenant)
        _ensure_job_rows(center, tenant_id)

    for step in PROVISIONING_STEPS:
        job = _get_or_create_job(center, tenant_id, step.value)
        result.steps = _job_steps(center, tenant_id)
        if job.status == ProvisioningStatus.SUCCEEDED.value:
            continue
        handler = _STEP_HANDLERS[step]
        _mark_job_running(center, job)
        result.steps = _job_steps(center, tenant_id)
        try:
            handler(context)
        except AppError as exc:
            _mark_job_failed(center, job, exc.message)
            _fail_tenant(center, tenant_id)
            result.failed_step = step.value
            result.error_message = exc.message
            result.status = TenantStatus.PROVISION_FAILED.value
            result.steps = _job_steps(center, tenant_id)
            logger.warning("租户开通失败 tenant=%s step=%s: %s", tenant_id, step.value, exc.message)
            return result
        except Exception as exc:  # noqa: BLE001 - 未预期异常同样记录到任务并置失败
            message = _safe_error_message(exc)
            _mark_job_failed(center, job, message)
            _fail_tenant(center, tenant_id)
            result.failed_step = step.value
            result.error_message = message
            result.status = TenantStatus.PROVISION_FAILED.value
            result.steps = _job_steps(center, tenant_id)
            logger.exception("租户开通异常 tenant=%s step=%s", tenant_id, step.value)
            return result
        _mark_job_succeeded(center, job)
        result.status = ProvisioningStatus.SUCCEEDED.value

    center.refresh(tenant)
    if tenant.status != TenantStatus.ACTIVE.value:
        # 全部步骤均成功但租户仍未激活（例如上次中断在激活之后）：补一次激活
        jobs_done = all(
            job.status == ProvisioningStatus.SUCCEEDED.value
            for job in repo.list_provisioning_jobs(center, tenant_id)
        )
        if jobs_done:
            _step_activate(context)
            center.refresh(tenant)
    result.status = tenant.status
    result.finished = True
    result.steps = _job_steps(center, tenant_id)
    return result


def retry_provisioning(center: Session, tenant_id: int, *, operator_user_id: int | None = None) -> ProvisioningResult:
    """重试开通：失败步骤重置为 pending 后重新执行（已成功步骤保持跳过）。"""
    tenant = repo.get_tenant(center, tenant_id)
    if tenant is None:
        raise AppError(ErrorCode.TENANT_NOT_FOUND)
    if tenant.status not in {TenantStatus.PROVISION_FAILED.value, TenantStatus.PROVISIONING.value}:
        raise AppError(ErrorCode.UNSUPPORTED_ACTION, "只有开通中或开通失败的租户可以重试")
    with run_in_transaction(center):
        for job in repo.list_provisioning_jobs(center, tenant_id):
            if job.status == ProvisioningStatus.FAILED.value:
                job.status = ProvisioningStatus.PENDING.value
                job.error_message = None
                job.started_at = None
                job.finished_at = None
                center.add(job)
    return provision_tenant(center, tenant_id, operator_user_id=operator_user_id)


def _ensure_job_rows(center: Session, tenant_id: int) -> None:
    existing = {job.step for job in repo.list_provisioning_jobs(center, tenant_id)}
    missing = [
        ProvisioningJob(
            tenant_id=tenant_id,
            step=step.value,
            status=ProvisioningStatus.PENDING.value,
            attempts=0,
        )
        for step in PROVISIONING_STEPS
        if step.value not in existing
    ]
    if missing:
        center.add_all(missing)
        center.flush()


def _get_or_create_job(center: Session, tenant_id: int, step: str) -> ProvisioningJob:
    job = center.execute(
        select(ProvisioningJob)
        .where(ProvisioningJob.tenant_id == tenant_id, ProvisioningJob.step == step)
        .order_by(ProvisioningJob.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if job is None:
        job = ProvisioningJob(
            tenant_id=tenant_id,
            step=step,
            status=ProvisioningStatus.PENDING.value,
            attempts=0,
        )
        center.add(job)
        center.flush()
    return job


def _mark_job_running(center: Session, job: ProvisioningJob) -> None:
    with run_in_transaction(center):
        job.status = ProvisioningStatus.RUNNING.value
        job.attempts = int(job.attempts or 0) + 1
        job.started_at = utcnow()
        job.finished_at = None
        job.error_message = None
        center.add(job)


def _mark_job_succeeded(center: Session, job: ProvisioningJob) -> None:
    with run_in_transaction(center):
        job.status = ProvisioningStatus.SUCCEEDED.value
        job.finished_at = utcnow()
        job.error_message = None
        center.add(job)


def _mark_job_failed(center: Session, job: ProvisioningJob, message: str) -> None:
    with run_in_transaction(center):
        job.status = ProvisioningStatus.FAILED.value
        job.finished_at = utcnow()
        job.error_message = (message or "")[:2000]
        center.add(job)


def _fail_tenant(center: Session, tenant_id: int) -> None:
    tenant = repo.get_tenant(center, tenant_id)
    if tenant is None:
        return
    with run_in_transaction(center):
        tenant.status = TenantStatus.PROVISION_FAILED.value
        center.add(tenant)


def _job_steps(center: Session, tenant_id: int) -> list[dict[str, Any]]:
    return [
        {
            "id": str(job.id),
            "step": job.step,
            "label": PROVISIONING_STEP_LABELS.get(job.step, job.step),
            "status": job.status,
            "attempts": job.attempts,
            "error_message": job.error_message,
            "started_at": _iso(job.started_at),
            "finished_at": _iso(job.finished_at),
        }
        for job in repo.list_provisioning_jobs(center, tenant_id)
    ]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    from datetime import timezone

    return value.replace(tzinfo=timezone.utc).isoformat()


def _safe_error_message(exc: Exception) -> str:
    message = str(exc) or exc.__class__.__name__
    return _mask_url(message)[:2000]


def _mask_url(value: str) -> str:
    """日志 / 任务错误信息中隐藏数据库 URL 密码。"""
    if "://" not in value:
        return value
    return _URL_CREDENTIALS_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}:***@", value)


# --------------------------------------------------------------------------- 步骤实现
def _step_register(context: _ProvisionContext) -> None:
    """中心库登记校验：确认租户行、负责人成员与开通任务行存在。"""
    tenant = context.tenant
    if tenant.owner_user_id is None:
        if not context.owner_email:
            raise AppError(ErrorCode.VALIDATION_ERROR, "缺少租户负责人")
        owner = _ensure_user(context.center, context.owner_email)
        with run_in_transaction(context.center):
            tenant.owner_user_id = int(owner.id)
            context.center.add(tenant)
    owner = repo.get_user(context.center, int(tenant.owner_user_id))
    if owner is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "负责人账号不存在")
    context.owner_email = owner.email
    with run_in_transaction(context.center):
        _ensure_owner_membership(context.center, int(tenant.id), int(owner.id))
        _ensure_job_rows(context.center, int(tenant.id))


def _step_create_database(context: _ProvisionContext) -> None:
    """创建业务库与受限账号（幂等：库 / 账号已存在时只更新凭据）。"""
    tenant = context.tenant
    database = repo.get_tenant_database(context.center, int(tenant.id))
    database_name = _tenant_database_name(tenant.code)
    username = _tenant_database_username(int(tenant.id))
    if database is not None and database.status == TENANT_DATABASE_READY and database.password_cipher:
        # 已存在可用路由：沿用原凭据并重放幂等 DDL（库被误删时也能恢复）
        try:
            password = decrypt_secret(database.password_cipher)
        except Exception:  # noqa: BLE001 - 密文失效时重建凭据
            logger.warning("租户 %s 数据库凭据失效，重新生成", tenant.id)
        else:
            _create_database_and_user(database.database_name, database.username, password)
            context.state["route_ready"] = True
            return
    password = random_token(24)
    _create_database_and_user(database_name, username, password)
    with run_in_transaction(context.center):
        if database is None:
            database = TenantDatabase(
                tenant_id=int(tenant.id),
                host=settings.tenant_db_host,
                port=int(settings.tenant_db_port),
                database_name=database_name,
                username=username,
                password_cipher=encrypt_secret(password),
                credentials_version=1,
                schema_version="",
                status=TENANT_DATABASE_READY,
                last_migrated_at=None,
            )
            context.center.add(database)
        else:
            database.host = settings.tenant_db_host
            database.port = int(settings.tenant_db_port)
            database.database_name = database_name
            database.username = username
            database.password_cipher = encrypt_secret(password)
            database.credentials_version = int(database.credentials_version or 1) + 1
            database.status = TENANT_DATABASE_READY
            context.center.add(database)
        context.center.flush()
    tenant_engines.invalidate_tenant(int(tenant.id))


def _tenant_database_name(code: str) -> str:
    suffix = re.sub(r"[^a-z0-9_]", "_", (code or "").lower())
    return _ensure_identifier(f"{settings.tenant_db_prefix}{suffix}")


def _tenant_database_username(tenant_id: int) -> str:
    return _ensure_identifier(f"{settings.tenant_db_user_prefix}{tenant_id}")


def _ensure_identifier(value: str) -> str:
    candidate = (value or "").strip()
    if not _DB_IDENTIFIER_PATTERN.match(candidate):
        raise AppError(ErrorCode.VALIDATION_ERROR, f"非法数据库标识: {candidate}")
    return candidate


def _create_database_and_user(database_name: str, username: str, password: str) -> None:
    """运维身份执行建库建号；账号只授予本库 DML 权限。"""
    database_name = _ensure_identifier(database_name)
    username = _ensure_identifier(username)
    engine = provisioner_engine()
    statements = [
        f"CREATE DATABASE IF NOT EXISTS `{database_name}` "
        "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci",
        f"CREATE USER IF NOT EXISTS '{username}'@'%' IDENTIFIED BY '{password}'",
        f"ALTER USER '{username}'@'%' IDENTIFIED BY '{password}'",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{database_name}`.* TO '{username}'@'%'",
        "FLUSH PRIVILEGES",
    ]
    with engine.connect() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _step_run_migrations(context: _ProvisionContext) -> None:
    """执行租户库迁移（独立子进程，使用运维身份连接）。"""
    tenant = context.tenant
    database = repo.get_tenant_database(context.center, int(tenant.id))
    if database is None:
        raise AppError(ErrorCode.TENANT_DATABASE_UNAVAILABLE, "缺少租户数据库路由")
    db_url = _migration_url(database.database_name)
    command = [
        sys.executable,
        "-m",
        "alembic",
        "-c",
        "alembic/tenant.ini",
        "-x",
        f"db_url={db_url}",
        "upgrade",
        "head",
    ]
    completed = subprocess.run(
        command,
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-8:]
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "租户库迁移失败：" + _mask_url(" | ".join(tail))[:800],
        )
    schema_version = _read_schema_version(database.database_name)
    with run_in_transaction(context.center):
        database.schema_version = schema_version
        database.last_migrated_at = utcnow()
        database.status = TENANT_DATABASE_READY
        context.center.add(database)


def _migration_url(database_name: str) -> str:
    """迁移使用运维身份连接目标租户库（业务账号没有 DDL 权限）。"""
    base_url = settings.provisioner_database_url or settings.center_database_url
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database_name}", parts.query, ""))


def _read_schema_version(database_name: str) -> str:
    try:
        with provisioner_engine().connect() as connection:
            value = connection.execute(
                text(f"SELECT version_num FROM `{_ensure_identifier(database_name)}`.alembic_version LIMIT 1")
            ).scalar()
        return str(value or "")
    except Exception:  # noqa: BLE001 - 版本读取失败不影响开通
        logger.warning("读取租户库 schema 版本失败 db=%s", database_name, exc_info=True)
        return ""


def _tenant_db_session(context: _ProvisionContext):
    """按中心库路由打开租户库会话（流水线内部使用）。"""
    from app.core.context import TenantDbRoute

    tenant = context.tenant
    database = repo.get_tenant_database(context.center, int(tenant.id))
    if database is None:
        raise AppError(ErrorCode.TENANT_DATABASE_UNAVAILABLE, "缺少租户数据库路由")
    route = TenantDbRoute(
        tenant_id=int(database.tenant_id),
        host=database.host,
        port=int(database.port),
        database=database.database_name,
        username=database.username,
        password=decrypt_secret(database.password_cipher),
        credentials_version=int(database.credentials_version or 1),
    )
    return tenant_session(route)


def _step_init_settings(context: _ProvisionContext) -> None:
    """初始化租户设置（权限版本、时区、同步开关）与功能开关。"""
    tenant = context.tenant
    with _tenant_db_session(context) as db:
        with run_in_transaction(db):
            ensure_tenant_settings(db, int(tenant.id), timezone=tenant.timezone or "Asia/Shanghai")
            from app.modules.access.models_tenant import FeatureSetting

            enabled_codes: set[str] = set(
                db.execute(
                    text("SELECT feature_code FROM feature_settings WHERE tenant_id = :tenant_id"),
                    {"tenant_id": int(tenant.id)},
                ).scalars()
            )
            for feature_code in DEFAULT_FEATURE_CODES:
                if feature_code in enabled_codes:
                    continue
                db.add(
                    FeatureSetting(
                        tenant_id=int(tenant.id),
                        feature_code=feature_code,
                        enabled=feature_code != "stock_push",
                        config=None,
                    )
                )


def _step_init_roles(context: _ProvisionContext) -> None:
    """写入预置角色，并给负责人绑定 owner 角色（全部数据范围）。"""
    tenant = context.tenant
    if tenant.owner_user_id is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "缺少租户负责人")
    membership = repo.get_membership(context.center, int(tenant.id), int(tenant.owner_user_id))
    if membership is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "负责人成员关系不存在")
    with _tenant_db_session(context) as db:
        with run_in_transaction(db):
            roles = seed_predefined_roles(db, int(tenant.id))
            owner_role = roles.get("owner")
            if owner_role is None:
                raise AppError(ErrorCode.INTERNAL_ERROR, "预置所有者角色缺失")
            existing = [
                row[1]
                for row in repo.list_member_role_rows(db, int(tenant.id), [int(membership.id)])
            ]
            if not existing:
                from app.modules.identity.service import _write_bindings

                _write_bindings(
                    db,
                    tenant_id=int(tenant.id),
                    member_id=int(membership.id),
                    bindings=[
                        RoleBindingChoice(
                            role_id=int(owner_role.id),
                            scopes=(ScopeChoice(scope_type="all", scope_ids=()),),
                        )
                    ],
                    assigned_by=context.operator_user_id,
                )


def _step_init_stock_place(context: _ProvisionContext) -> None:
    """创建默认库存地点（幂等；库存地点表由 catalog/inventory 迁移创建）。"""
    tenant = context.tenant
    with _tenant_db_session(context) as db:
        with run_in_transaction(db):
            existing = db.execute(
                text("SELECT id FROM stock_places WHERE tenant_id = :tenant_id AND code = :code LIMIT 1"),
                {"tenant_id": int(tenant.id), "code": DEFAULT_STOCK_PLACE_CODE},
            ).first()
            if existing is not None:
                return
            db.execute(
                text(
                    "INSERT INTO stock_places (tenant_id, code, name, is_default, status) "
                    "VALUES (:tenant_id, :code, :name, :is_default, :status)"
                ),
                {
                    "tenant_id": int(tenant.id),
                    "code": DEFAULT_STOCK_PLACE_CODE,
                    "name": DEFAULT_STOCK_PLACE_NAME,
                    "is_default": True,
                    "status": "active",
                },
            )


def _step_init_subscription(context: _ProvisionContext) -> None:
    """绑定套餐与订阅；已有有效订阅时不重复创建。"""
    tenant = context.tenant
    plan_id = int(tenant.plan_id) if tenant.plan_id else None
    with run_in_transaction(context.center):
        plan = context.center.get(Plan, plan_id) if plan_id else None
        if plan is None:
            plan = ensure_default_plan(context.center)
            tenant.plan_id = int(plan.id)
            context.center.add(tenant)
        existing = context.center.execute(
            text(
                "SELECT id FROM subscriptions WHERE tenant_id = :tenant_id AND status = 'active' LIMIT 1"
            ),
            {"tenant_id": int(tenant.id)},
        ).first()
        if existing is None:
            context.center.add(
                Subscription(
                    tenant_id=int(tenant.id),
                    plan_id=int(plan.id),
                    status="active",
                    starts_at=utcnow(),
                    ends_at=None,
                )
            )
            context.center.flush()


def _step_invite_owner(context: _ProvisionContext) -> None:
    """撤销旧邀请并发出新的负责人邀请（令牌原文只返回一次）。"""
    from app.core.config import settings as app_settings

    tenant = context.tenant
    if tenant.owner_user_id is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "缺少租户负责人")
    owner = repo.get_user(context.center, int(tenant.owner_user_id))
    if owner is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "负责人账号不存在")
    membership = repo.get_membership(context.center, int(tenant.id), int(owner.id))
    if membership is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "负责人成员关系不存在")
    token = random_token()
    with _tenant_db_session(context) as db:
        roles = {role.code: role for role in repo.list_roles(db, int(tenant.id), include_disabled=True)}
        owner_role = roles.get("owner")
        role_binding_json = (
            [
                {
                    "role_id": int(owner_role.id),
                    "scopes": [{"scope_type": "all", "scope_ids": []}],
                }
            ]
            if owner_role is not None
            else []
        )
    with run_in_transaction(context.center):
        for stale in repo.list_pending_invitations(context.center, int(tenant.id), owner.email):
            stale.status = InvitationStatus.REVOKED.value
            context.center.add(stale)
        context.center.add(
            TenantInvitation(
                tenant_id=int(tenant.id),
                email=owner.email,
                token_hash=sha256_hex(token),
                role_ids=role_binding_json,
                status=InvitationStatus.PENDING.value,
                expires_at=_utc_after_seconds(int(app_settings.invitation_ttl_seconds)),
                invited_by=context.operator_user_id,
            )
        )
        context.center.flush()
    if context.result is not None:
        context.result.owner_invitation_token = token
    context.state["owner_invitation_token"] = token
    # 邀请已发出，负责人成员关系保持 invited，等待激活后变为 active
    # 已激活的负责人（重试开通）不回退成员状态
    if membership.status != MembershipStatus.ACTIVE.value:
        with run_in_transaction(context.center):
            membership.status = MembershipStatus.INVITED.value
            context.center.add(membership)


def _utc_after_seconds(seconds: int) -> datetime:
    from datetime import timedelta

    return utcnow() + timedelta(seconds=seconds)


def _step_activate(context: _ProvisionContext) -> None:
    """标记租户 active（所有步骤成功后才会执行到这里）。"""
    tenant = repo.get_tenant(context.center, int(context.tenant.id))
    if tenant is None:
        raise AppError(ErrorCode.TENANT_NOT_FOUND)
    with run_in_transaction(context.center):
        tenant.status = TenantStatus.ACTIVE.value
        tenant.suspended_at = None
        tenant.suspended_reason = None
        context.center.add(tenant)


_STEP_HANDLERS: dict[ProvisioningStep, Callable[[_ProvisionContext], None]] = {
    ProvisioningStep.REGISTER: _step_register,
    ProvisioningStep.CREATE_DATABASE: _step_create_database,
    ProvisioningStep.RUN_MIGRATIONS: _step_run_migrations,
    ProvisioningStep.INIT_SETTINGS: _step_init_settings,
    ProvisioningStep.INIT_ROLES: _step_init_roles,
    ProvisioningStep.INIT_STOCK_PLACE: _step_init_stock_place,
    ProvisioningStep.INIT_SUBSCRIPTION: _step_init_subscription,
    ProvisioningStep.INVITE_OWNER: _step_invite_owner,
    ProvisioningStep.ACTIVATE: _step_activate,
}


# --------------------------------------------------------------------------- 平台辅助
def ensure_instance_allows_center_write(center: Session, tenant_id: int, *, action: str) -> None:
    """批次2：已独立部署 / 迁移中的租户只能走迁移流程，不能用普通启用/开通恢复写入。

    ``deployment_mode=independent`` 或 ``migration_status in (migrating, migrated, rollback)``
    时抛 409 CONFLICT；避免旧中心把已迁出实例重新激活造成双写。
    """
    try:
        from app.modules.ops import service as ops_service
    except Exception:  # noqa: BLE001 - 运维模块未加载时保持旧行为（中心模式恒定可用）
        logger.warning("ops.service 不可用，跳过实例迁移状态校验 tenant=%s", tenant_id)
        return
    if ops_service.tenant_scheduling_blocked(center, tenant_id):
        raise AppError(
            ErrorCode.CONFLICT,
            f"租户实例已独立部署或处于迁移状态，不能用普通「{action}」恢复写入，请通过迁移流程操作",
        )


def suspend_tenant(center: Session, *, tenant_id: int, reason: str, operator_user_id: int | None = None) -> Tenant:
    """暂停租户：禁止租户业务访问与主动同步；已验签回调可暂存但不改库存。"""
    tenant = repo.get_tenant(center, tenant_id)
    if tenant is None:
        raise AppError(ErrorCode.TENANT_NOT_FOUND)
    if tenant.status == TenantStatus.SUSPENDED.value:
        return tenant
    if tenant.status not in {TenantStatus.ACTIVE.value, TenantStatus.PROVISION_FAILED.value}:
        raise AppError(ErrorCode.UNSUPPORTED_ACTION, "当前状态不能暂停")
    with run_in_transaction(center):
        tenant.status = TenantStatus.SUSPENDED.value
        tenant.suspended_at = utcnow()
        tenant.suspended_reason = (reason or "")[:255]
        center.add(tenant)
    logger.info("租户已暂停 tenant=%s operator=%s reason=%s", tenant_id, operator_user_id, reason)
    return tenant


def activate_tenant(center: Session, *, tenant_id: int, operator_user_id: int | None = None) -> Tenant:
    """重新启用租户：先核对开通状态，再恢复可用。"""
    tenant = repo.get_tenant(center, tenant_id)
    if tenant is None:
        raise AppError(ErrorCode.TENANT_NOT_FOUND)
    if tenant.status == TenantStatus.ACTIVE.value:
        return tenant
    if tenant.status != TenantStatus.SUSPENDED.value:
        raise AppError(ErrorCode.UNSUPPORTED_ACTION, "只有已暂停的租户可以重新启用")
    # 批次2：已独立部署 / 迁移中的实例不能通过普通启用恢复写入
    ensure_instance_allows_center_write(center, tenant_id, action="启用租户")
    database = repo.get_tenant_database(center, tenant_id)
    if database is None or database.status != TENANT_DATABASE_READY:
        raise AppError(ErrorCode.TENANT_DATABASE_UNAVAILABLE, "租户数据库不可用，无法启用")
    with run_in_transaction(center):
        tenant.status = TenantStatus.ACTIVE.value
        tenant.suspended_at = None
        tenant.suspended_reason = None
        center.add(tenant)
    logger.info("租户已启用 tenant=%s operator=%s", tenant_id, operator_user_id)
    return tenant


def run_provisioning(center: Session, tenant_id: int, *, operator_user_id: int | None = None) -> dict[str, Any]:
    """同步执行开通流水线并返回结果字典（CLI / 降级路径使用）。"""
    result = provision_tenant(center, tenant_id, operator_user_id=operator_user_id)
    return result.as_dict()


# --------------------------------------------------------------------------- 异步投递
def dispatch_provisioning(
    center: Session,
    tenant_id: int,
    *,
    operator_user_id: int | None = None,
    retry: bool = False,
) -> dict[str, Any]:
    """投递开通 / 重试任务到 Celery，HTTP 请求不等待流水线执行。

    返回结构::

        {"mode": "async" | "sync-eager" | "sync-degraded",
         "task_id": "<Celery 任务ID 或 provisioning_jobs.id>",
         "provisioning_job_id": "<provisioning_jobs.id>",
         "provisioning": {开通结果} | None}

    降级策略（保证本机 / 无 broker 环境可用）：

    * ``settings.celery_task_always_eager`` 为真：直接同步执行（测试 / 本机）；
    * Celery 投递失败（broker 不可用）：记录 warning 后同步执行。
    """
    jobs = repo.list_provisioning_jobs(center, tenant_id)
    job_id = str(jobs[0].id) if jobs else str(tenant_id)
    if settings.celery_task_always_eager:
        logger.warning("Celery 处于 eager 模式，开通任务同步执行 tenant=%s", tenant_id)
        return _run_sync_provisioning(
            center, tenant_id, operator_user_id, retry=retry, mode="sync-eager", job_id=job_id
        )
    try:
        task = _identity_task(retry=retry)
        async_result = task.apply_async(
            args=[int(tenant_id)],
            kwargs={"operator_user_id": operator_user_id},
            queue=PROVISIONING_QUEUE,
            retry=False,
        )
    except Exception as exc:  # noqa: BLE001 - broker 不可用时降级同步执行
        logger.warning("Celery 投递开通任务失败，降级同步执行 tenant=%s: %s", tenant_id, exc)
        return _run_sync_provisioning(
            center, tenant_id, operator_user_id, retry=retry, mode="sync-degraded", job_id=job_id
        )
    logger.info(
        "已投递租户开通任务 tenant=%s task=%s mode=%s",
        tenant_id,
        async_result.id,
        "retry" if retry else "provision",
    )
    return {
        "mode": "async",
        "task_id": str(async_result.id),
        "provisioning_job_id": job_id,
        "provisioning": None,
    }


def _identity_task(*, retry: bool):
    """延迟导入 Celery 任务，避免无 broker 环境 / 未加入 include 时导入失败。"""
    from app.modules.identity import tasks as identity_tasks

    return identity_tasks.retry_provision_job if retry else identity_tasks.provision_tenant_job


def _run_sync_provisioning(
    center: Session,
    tenant_id: int,
    operator_user_id: int | None,
    *,
    retry: bool,
    mode: str,
    job_id: str,
) -> dict[str, Any]:
    if retry:
        result = retry_provisioning(center, tenant_id, operator_user_id=operator_user_id)
    else:
        result = provision_tenant(center, tenant_id, operator_user_id=operator_user_id)
    return {
        "mode": mode,
        "task_id": job_id,
        "provisioning_job_id": job_id,
        "provisioning": result.as_dict(),
    }
