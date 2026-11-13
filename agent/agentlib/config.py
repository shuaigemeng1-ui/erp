"""Agent 配置读写（仅标准库）。

配置固定放在 ``/etc/erp-agent/config.json``（0600/0640），**不随 Agent 版本目录切换**，
因此 Agent 自升级后节点 UUID、节点令牌与部署路径全部保留。

向后兼容：旧版配置只有 ``center/node_id/node_uuid/node_token/instance_id``，
新版读取时补齐默认值，但写回时保留调用方未识别的键。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .constants import (
    AGENT_VERSION,
    DEFAULT_AGENT_ROOT,
    DEFAULT_CONFIG,
    DEFAULT_DEPLOY_DIR,
    DEFAULT_REPO_DIR,
    DEFAULT_STATE_DIR,
    HEARTBEAT_SECONDS,
    JOB_LEASE_RENEW_SECONDS,
    JOB_LEASE_SECONDS,
    JOB_POLL_SECONDS,
    PROTOCOL_VERSION,
    RESULT_REPORT_SECONDS,
    TELEMETRY_SECONDS,
)

DEFAULT_KEYS: dict[str, Any] = {
    # 与后端现有字段保持一致
    "center": "",
    "node_id": None,
    "node_uuid": "",
    "node_token": "",
    "instance_id": None,
    # 部署与路径
    "agent_root": DEFAULT_AGENT_ROOT,
    "state_dir": DEFAULT_STATE_DIR,
    "deploy_dir": DEFAULT_DEPLOY_DIR,
    "repo_dir": DEFAULT_REPO_DIR,
    "versions_dir": "",  # 为空时使用 <agent_root>/versions
    "compose_file": "docker-compose.yml",
    "compose_server_file": "docker-compose.server.yml",
    "compose_project": "ventasync",
    "env_file": ".env",
    "units": ["erp-agent.service", "erp-agent-executor.service"],
    # 时间参数（秒）
    "telemetry_seconds": TELEMETRY_SECONDS,
    "heartbeat_seconds": HEARTBEAT_SECONDS,
    "job_poll_seconds": JOB_POLL_SECONDS,
    "job_lease_seconds": JOB_LEASE_SECONDS,
    "job_lease_renew_seconds": JOB_LEASE_RENEW_SECONDS,
    "result_report_seconds": RESULT_REPORT_SECONDS,
    "protocol_version": PROTOCOL_VERSION,
    "agent_version": AGENT_VERSION,
}


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """读取配置并与默认值合并；文件不存在时返回默认值（用于自测/手工命令）。"""
    raw: dict[str, Any] = {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    config = dict(DEFAULT_KEYS)
    if isinstance(raw, dict):
        config.update(raw)
    return config


def save_config(path: str | Path, config: dict[str, Any]) -> None:
    """原子写入配置并收紧权限；不清除调用方未识别的键。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(target.parent), prefix=".config-", delete=False
    )
    try:
        with handle:
            handle.write(payload)
        os.replace(handle.name, target)
    finally:
        try:
            if os.path.exists(handle.name):
                os.unlink(handle.name)
        except OSError:
            pass
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def state_dir(config: dict[str, Any]) -> Path:
    return Path(str(config.get("state_dir") or DEFAULT_STATE_DIR))


def journal_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "erp-agent.db"


def telemetry_cache_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "telemetry-cache.json"


def agent_root(config: dict[str, Any]) -> Path:
    return Path(str(config.get("agent_root") or DEFAULT_AGENT_ROOT))


def versions_root(config: dict[str, Any]) -> Path:
    custom = str(config.get("versions_dir") or "").strip()
    return Path(custom) if custom else agent_root(config) / "versions"


def deploy_dir(config: dict[str, Any]) -> Path:
    return Path(str(config.get("deploy_dir") or DEFAULT_DEPLOY_DIR))


def repo_dir(config: dict[str, Any]) -> Path:
    return Path(str(config.get("repo_dir") or DEFAULT_REPO_DIR))


def center_url(config: dict[str, Any]) -> str:
    return str(config.get("center") or "").rstrip("/")


def node_token(config: dict[str, Any]) -> str:
    return str(config.get("node_token") or "")


def units(config: dict[str, Any]) -> list[str]:
    value = config.get("units") or list(DEFAULT_KEYS["units"])
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item) for item in value]


def ensure_state_dir(config: dict[str, Any]) -> Path:
    target = state_dir(config)
    target.mkdir(parents=True, exist_ok=True)
    return target
