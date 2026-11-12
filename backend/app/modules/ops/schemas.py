"""统一运维中心请求/响应模型。"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DOMAIN_RE = re.compile(r"^(?=.{1,255}$)(?!-)[a-zA-Z0-9-]{1,63}(?<!-)(\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))+$")


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
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    version: str = Field(min_length=1, max_length=64)
    image_tag: str = Field(default="", max_length=255)
    image_digest: str = Field(default="", max_length=255)
    notes: str = Field(default="", max_length=500)


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


class MaintenanceWindowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    starts_at: datetime
    ends_at: datetime
    reason: str = Field(default="", max_length=255)
