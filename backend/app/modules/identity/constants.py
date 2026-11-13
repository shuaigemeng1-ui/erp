"""身份与租户模块的稳定常量（状态值、开通步骤、邀请有效期等）。

约定（见 docs/data-model.md）：

* 状态字段统一 ``VARCHAR(32)``，取值在代码中定义，不使用数据库 ``ENUM``；
* 状态值是前后端与审计共同引用的稳定契约，禁止散落字符串字面量。
"""

from __future__ import annotations

from enum import StrEnum


class UserStatus(StrEnum):
    """全局用户状态。"""

    ACTIVE = "active"
    DISABLED = "disabled"


class TenantStatus(StrEnum):
    """租户生命周期状态（plan.txt §2「租户生命周期」）。"""

    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    PROVISION_FAILED = "provision_failed"


# 允许租户内业务访问的状态集合（委托 deps 使用的判断依据）
TENANT_BLOCKED_STATUSES: frozenset[str] = frozenset(
    {TenantStatus.SUSPENDED.value, TenantStatus.PROVISION_FAILED.value}
)


class MembershipStatus(StrEnum):
    """租户成员关系状态。"""

    ACTIVE = "active"
    INVITED = "invited"
    DISABLED = "disabled"


class InvitationStatus(StrEnum):
    """邀请状态：待接受 / 已接受 / 已撤销 / 已过期。"""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ProvisioningStatus(StrEnum):
    """开通任务步骤状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ProvisioningStep(StrEnum):
    """开通流水线步骤（顺序即执行顺序，见 plan.txt §2「租户生命周期」）。"""

    REGISTER = "register"
    CREATE_DATABASE = "create_database"
    RUN_MIGRATIONS = "run_migrations"
    INIT_SETTINGS = "init_settings"
    INIT_ROLES = "init_roles"
    INIT_STOCK_PLACE = "init_stock_place"
    INIT_SUBSCRIPTION = "init_subscription"
    INVITE_OWNER = "invite_owner"
    ACTIVATE = "activate"


# 流水线执行顺序：注册 → 建库建号 → 迁移 → 初始化设置/角色/库存地点 → 订阅 → 邀请负责人 → 激活
PROVISIONING_STEPS: tuple[ProvisioningStep, ...] = (
    ProvisioningStep.REGISTER,
    ProvisioningStep.CREATE_DATABASE,
    ProvisioningStep.RUN_MIGRATIONS,
    ProvisioningStep.INIT_SETTINGS,
    ProvisioningStep.INIT_ROLES,
    ProvisioningStep.INIT_STOCK_PLACE,
    ProvisioningStep.INIT_SUBSCRIPTION,
    ProvisioningStep.INVITE_OWNER,
    ProvisioningStep.ACTIVATE,
)

# 步骤对应的中文说明（写入 provisioning_jobs 的说明与平台详情展示）
PROVISIONING_STEP_LABELS: dict[str, str] = {
    ProvisioningStep.REGISTER: "中心库登记租户",
    ProvisioningStep.CREATE_DATABASE: "创建租户业务库与受限账号",
    ProvisioningStep.RUN_MIGRATIONS: "执行租户库迁移",
    ProvisioningStep.INIT_SETTINGS: "初始化租户设置与功能开关",
    ProvisioningStep.INIT_ROLES: "预置租户角色与权限",
    ProvisioningStep.INIT_STOCK_PLACE: "创建默认库存地点",
    ProvisioningStep.INIT_SUBSCRIPTION: "绑定套餐与订阅",
    ProvisioningStep.INVITE_OWNER: "邀请租户负责人",
    ProvisioningStep.ACTIVATE: "标记租户为可用",
}

# 租户库连接账号状态
TENANT_DATABASE_READY = "ready"

# 预置库存地点编码（catalog/inventory 模块按编码识别默认地点）
DEFAULT_STOCK_PLACE_CODE = "default"
DEFAULT_STOCK_PLACE_NAME = "默认库存地点"

# 平台角色编码（core.permissions.PlatformRole）
PLATFORM_ROLE_ADMIN = "platform_admin"
PLATFORM_ROLE_OPS = "platform_ops"

# 平台审计动作
PLATFORM_AUDIT_ACTIONS: dict[str, str] = {
    "tenant.create": "创建租户",
    "tenant.provision": "发起开通",
    "tenant.retry_provision": "重试开通",
    "tenant.suspend": "暂停租户",
    "tenant.activate": "启用租户",
    "tenant.owner_transfer": "转移租户所有者",
    "platform.tenant.owner_invitation": "重新签发负责人邀请",
    "plan.create": "创建套餐",
    "plan.update": "调整套餐",
    "channel_app.update": "保存渠道应用",
    "instance.create": "登记租户实例",
    "instance.update": "更新租户实例",
    "instance.node.enroll_token": "生成服务器接入令牌",
    "alert.acknowledge": "确认运维告警",
    "release.create": "登记发布版本",
    "release.update": "更新发布版本",
    "ssh.config.update": "更新 SSH 配置",
    "ssh.connect": "SSH 连接成功",
    "ssh.connect.failed": "SSH 连接失败",
    "ssh.fingerprint.recorded": "记录 SSH 主机指纹",
    "ssh.fingerprint.mismatch": "SSH 主机指纹不匹配",
    "ssh.fingerprint.reset": "重置 SSH 主机指纹",
    "ssh.command": "执行 SSH 命令",
    "ssh.disconnect": "断开 SSH 连接",
    "ssh.terminal.open": "打开 SSH 终端",
    "ssh.terminal.open.failed": "打开 SSH 终端失败",
    "ssh.terminal.close": "关闭 SSH 终端",
    "task.outbox_rejected": "拒绝越权任务投递",
}


def is_valid_tenant_status(value: str) -> bool:
    """是否为已知租户状态（纯函数，供路由与测试使用）。"""
    return value in {item.value for item in TenantStatus}


def is_valid_membership_status(value: str) -> bool:
    return value in {item.value for item in MembershipStatus}
