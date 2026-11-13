"""统一运维中心请求/响应模型。"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DOMAIN_RE = re.compile(r"^(?=.{1,255}$)(?!-)[a-zA-Z0-9-]{1,63}(?<!-)(\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))+$")
# 完整 Git 提交号：40 位（SHA-1）或 64 位（SHA-256）十六进制
COMMIT_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")

BUILD_STATUSES = ("built", "verified", "failed")
RELEASE_STATUSES = ("draft", "active", "retired")
UPGRADE_STATUSES = (
    "pending",
    "building",
    "maintenance",
    "migrating",
    "verifying",
    "succeeded",
    "failed",
    "unknown",
)


def normalize_commit_sha(value: str) -> str:
    """校验并规范化完整 Git commit（只接受 40/64 位十六进制），返回小写。"""
    normalized = (value or "").strip().lower()
    if not COMMIT_SHA_RE.fullmatch(normalized):
        raise ValueError("commit_sha 必须是完整的 40 或 64 位十六进制 Git 提交号")
    return normalized


class InstanceCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    tenant_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=100)
    domain: str = Field(min_length=3, max_length=255)
    environment: Literal["production", "test"] = "production"
    notes: str = Field(default="", max_length=500)

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        normalized = value.strip().lower().rstrip(".")
        if "://" in normalized or "/" in normalized or not DOMAIN_RE.fullmatch(normalized):
            raise ValueError("请输入合法域名，不含协议和路径")
        return normalized


class InstanceUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    domain: str | None = Field(default=None, min_length=3, max_length=255)
    status: Literal["pending", "active", "suspended"] | None = None
    deployment_mode: Literal["shared", "independent"] | None = None
    migration_status: Literal["registered", "migrating", "migrated", "rollback"] | None = None
    app_version: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower().rstrip(".")
        if "://" in normalized or "/" in normalized or not DOMAIN_RE.fullmatch(normalized):
            raise ValueError("请输入合法域名，不含协议和路径")
        return normalized


class NodeEnrollIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    token: str = Field(min_length=16, max_length=256)
    hostname: str = Field(default="", max_length=255)
    os_name: str = Field(default="", max_length=100)
    arch: str = Field(default="", max_length=32)
    cpu_cores: int = Field(default=0, ge=0, le=4096)
    memory_total_mb: int = Field(default=0, ge=0, le=10_000_000)
    disk_total_gb: int = Field(default=0, ge=0, le=10_000_000)
    internal_ips: list[str] = Field(default_factory=list, max_length=32)
    public_ip: str = Field(default="", max_length=64)
    agent_version: str = Field(default="", max_length=32)
    executor_version: str = Field(default="", max_length=32)
    protocol_version: int = Field(default=0, ge=0, le=10000)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    app_version: str = Field(default="", max_length=64)
    metadata: dict[str, Any] | None = None

    @field_validator("internal_ips")
    @classmethod
    def validate_internal_ips(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            try:
                ipaddress.ip_address(value)
            except ValueError as exc:
                raise ValueError(f"内网 IP 格式不正确：{value}") from exc
            if value not in result:
                result.append(value)
        return result


class TelemetryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sampled_at: datetime | None = None
    hostname: str = Field(default="", max_length=255)
    os_name: str = Field(default="", max_length=100)
    arch: str = Field(default="", max_length=32)
    cpu_cores: int = Field(default=0, ge=0, le=4096)
    memory_total_mb: int = Field(default=0, ge=0, le=10_000_000)
    disk_total_gb: int = Field(default=0, ge=0, le=10_000_000)
    internal_ips: list[str] = Field(default_factory=list, max_length=32)
    public_ip: str = Field(default="", max_length=64)
    agent_version: str = Field(default="", max_length=32)
    executor_version: str = Field(default="", max_length=32)
    protocol_version: int = Field(default=0, ge=0, le=10000)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    db_version: str = Field(default="", max_length=64)
    app_version: str = Field(default="", max_length=64)

    cpu_percent: float = Field(default=0, ge=0, le=100)
    load1: float = Field(default=0, ge=0)
    load5: float = Field(default=0, ge=0)
    load15: float = Field(default=0, ge=0)
    memory_used_mb: int = Field(default=0, ge=0)
    memory_available_mb: int = Field(default=0, ge=0)
    memory_percent: float = Field(default=0, ge=0, le=100)
    swap_total_mb: int = Field(default=0, ge=0)
    swap_used_mb: int = Field(default=0, ge=0)
    disks: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    network: dict[str, Any] = Field(default_factory=dict)
    services: dict[str, Any] = Field(default_factory=dict)


class AlertAckIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    note: str = Field(default="", max_length=500)


class ReleaseCreate(BaseModel):
    """登记发布版本：必须携带完整 Git commit，构建/数据库目标版本可空。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    version: str = Field(min_length=1, max_length=64)
    image_tag: str = Field(default="", max_length=255)
    image_digest: str = Field(default="", max_length=255)
    commit_sha: str = Field(min_length=40, max_length=64, description="完整 Git 提交号（40/64 位十六进制）")
    build_config_summary: str = Field(default="", max_length=500)
    min_agent_protocol: int = Field(default=0, ge=0, le=10000)
    center_db_version: str = Field(default="", max_length=64)
    tenant_db_version: str = Field(default="", max_length=64)
    rollback_compatible: bool = Field(default=False)
    build_status: Literal["built", "verified", "failed"] = "built"
    status: Literal["draft", "active", "retired"] = "active"
    notes: str = Field(default="", max_length=500)

    @field_validator("commit_sha")
    @classmethod
    def validate_commit_sha(cls, value: str) -> str:
        return normalize_commit_sha(value)


class ReleaseUpdate(BaseModel):
    """更新发布版本：允许回填旧版本的完整 commit 与构建/数据库目标信息。

    所有字段可选（局部更新）；``commit_sha`` 提供时必须是 40/64 位十六进制。
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    commit_sha: str | None = Field(default=None, min_length=40, max_length=64)
    build_config_summary: str | None = Field(default=None, max_length=500)
    min_agent_protocol: int | None = Field(default=None, ge=0, le=10000)
    center_db_version: str | None = Field(default=None, max_length=64)
    tenant_db_version: str | None = Field(default=None, max_length=64)
    rollback_compatible: bool | None = None
    build_status: Literal["built", "verified", "failed"] | None = None
    status: Literal["draft", "active", "retired"] | None = None
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("commit_sha")
    @classmethod
    def validate_commit_sha(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_commit_sha(value)


class OperationCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    job_type: Literal["deploy", "upgrade", "restart", "backup", "logs", "config"]
    target_version: str = Field(default="", max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(default="", max_length=128)


class AgentJobEvent(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    status: str = Field(default="running", max_length=16)
    step_no: int | None = Field(default=None, ge=1, le=100)
    step_name: str = Field(default="", max_length=64)
    step_status: str = Field(default="", max_length=16)
    message: str = Field(default="", max_length=1000)
    result: dict[str, Any] | None = None
    error: str = Field(default="", max_length=500)
    # 执行批次与租约（新 Agent 携带；旧 Agent 为空时按兼容模式处理）
    attempt_id: str = Field(default="", max_length=36)
    lease_token: str = Field(default="", max_length=128)
    event_seq: int = Field(default=0, ge=0, le=1_000_000)
    agent_protocol_version: int = Field(default=0, ge=0, le=10000)
    # 升级校验结果：{"db_version": {"ok": true, "value": "...", "message": ""}, ...}
    checks: dict[str, Any] | None = None


class AgentHeartbeatIn(BaseModel):
    """Agent 心跳（任务执行期或空闲期）：协议/版本/能力/数据库版本。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_version: str = Field(default="", max_length=32)
    executor_version: str = Field(default="", max_length=32)
    protocol_version: int = Field(default=0, ge=0, le=10000)
    db_version: str = Field(default="", max_length=64)
    app_version: str = Field(default="", max_length=64)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    job_id: int | None = Field(default=None, ge=1)
    attempt_id: str = Field(default="", max_length=36)
    lease_token: str = Field(default="", max_length=128)
    event_seq: int = Field(default=0, ge=0, le=1_000_000)


class MaintenanceWindowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    starts_at: datetime
    ends_at: datetime
    reason: str = Field(default="", max_length=255)


class NodeSshConfigIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    host: str = Field(default="", max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)


class NodeSshExecIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    command: str = Field(min_length=1, max_length=2000)
