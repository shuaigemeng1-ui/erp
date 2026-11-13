#!/usr/bin/env python3
"""ERP Agent 本机受限执行器。

只接受管理中心下发的固定任务类型：
* restart：重启指定 compose 服务（api/worker/scheduler/nginx）
* backup：执行部署目录内固定的 backup.sh
* logs：读取指定 compose 服务最近日志并脱敏
* deploy/upgrade：拉取并启动固定 compose 项目

不执行任意 Shell，不接受用户拼接命令。所有命令使用参数列表调用。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

ALLOWED_SERVICES = {"api", "worker", "scheduler", "nginx"}
ALLOWED_ACTIONS = {"restart", "backup", "logs", "deploy", "upgrade"}
SECRET_PATTERNS = [
    re.compile(r"(?i)(password|passwd|secret|token|access[_-]?key|secret[_-]?key)\s*[=:]\s*\S+"),
    re.compile(r"(?i)(authorization:\s*bearer)\s+\S+"),
]


def redact(text: str) -> str:
    value = text
    for pattern in SECRET_PATTERNS:
        value = pattern.sub(lambda m: f"{m.group(1)}=***", value)
    return value[-4000:]


def _run(command: list[str], cwd: str, timeout: int = 300, extra_env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "COMPOSE_INTERACTIVE_NO_CLI": "1", **(extra_env or {})},
        )
        output = redact((result.stdout or "") + "\n" + (result.stderr or ""))
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return 124, "任务执行超时"
    except OSError as exc:
        return 1, redact(str(exc))


def _compose(config: dict[str, Any]) -> list[str]:
    deploy_dir = Path(str(config.get("deploy_dir") or "/opt/ventasync/deploy"))
    compose_file = str(config.get("compose_file") or "docker-compose.yml")
    compose_server = str(config.get("compose_server_file") or "docker-compose.server.yml")
    env_file = str(config.get("env_file") or ".env")
    return [
        "docker",
        "compose",
        "-f",
        str(deploy_dir / compose_file),
        "-f",
        str(deploy_dir / compose_server),
        "--env-file",
        str(deploy_dir / env_file),
    ]


def execute(job: dict[str, Any], config: dict[str, Any], report) -> dict[str, Any]:
    job_type = str(job.get("job_type") or "")
    payload = job.get("payload") or {}
    if job_type not in ALLOWED_ACTIONS:
        report(1, "校验任务", "failed", f"不支持的任务类型：{job_type}")
        return {"ok": False, "error": "unsupported_job_type"}
    deploy_dir = str(Path(str(config.get("deploy_dir") or "/opt/ventasync/deploy")))
    compose = _compose(config)
    report(1, "检查任务参数", "succeeded", f"任务类型：{job_type}")
    if job_type == "restart":
        service = str(payload.get("service") or "api")
        if service not in ALLOWED_SERVICES:
            report(2, "校验服务", "failed", "服务不在允许列表")
            return {"ok": False, "error": "service_not_allowed"}
        report(2, f"重启 {service}", "running", "")
        code, output = _run([*compose, "restart", service], deploy_dir, timeout=180)
        report(2, f"重启 {service}", "succeeded" if code == 0 else "failed", output)
        return {"ok": code == 0, "exit_code": code, "output": output}
    if job_type == "logs":
        service = str(payload.get("service") or "api")
        if service not in ALLOWED_SERVICES:
            report(2, "校验服务", "failed", "服务不在允许列表")
            return {"ok": False, "error": "service_not_allowed"}
        tail = max(10, min(int(payload.get("tail") or 200), 500))
        code, output = _run([*compose, "logs", "--tail", str(tail), service], deploy_dir, timeout=60)
        report(2, f"读取 {service} 日志", "succeeded" if code == 0 else "failed", output)
        return {"ok": code == 0, "exit_code": code, "output": output}
    if job_type == "backup":
        script = Path(deploy_dir) / "backup.sh"
        if not script.exists():
            report(2, "执行备份", "failed", "backup.sh 不存在")
            return {"ok": False, "error": "backup_script_missing"}
        report(2, "执行备份", "running", "")
        code, output = _run(["bash", str(script)], deploy_dir, timeout=3600)
        size = 0
        key = ""
        match = re.search(r"BACKUP_KEY=(\S+)", output)
        if match:
            key = match.group(1)
        size_match = re.search(r"BACKUP_SIZE=(\d+)", output)
        if size_match:
            size = int(size_match.group(1))
        report(2, "执行备份", "succeeded" if code == 0 else "failed", output)
        return {"ok": code == 0, "exit_code": code, "output": output, "storage_key": key, "size_bytes": size}
    # deploy / upgrade：目标版本作为 APP_IMAGE_TAG 传入，不修改 .env；固定版本镜像可本地存在。
    target = str(job.get("target_version") or payload.get("target_version") or "").strip()
    if target and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", target):
        report(2, "校验版本", "failed", "版本号格式不正确")
        return {"ok": False, "error": "invalid_version"}
    env_extra = {"APP_IMAGE_TAG": target} if target else {}
    report(2, f"拉取镜像 {target or '当前版本'}", "running", "")
    code, output = _run([*compose, "pull", "--ignore-pull-failures"], deploy_dir, timeout=1800, extra_env=env_extra)
    report(2, "拉取镜像", "succeeded" if code == 0 else "failed", output)
    if code != 0:
        return {"ok": False, "exit_code": code, "output": output}
    report(3, f"更新服务 {target or '当前版本'}", "running", "")
    code2, output2 = _run([*compose, "up", "-d", "--remove-orphans"], deploy_dir, timeout=1800, extra_env=env_extra)
    report(3, "更新服务", "succeeded" if code2 == 0 else "failed", output2)
    time.sleep(3)
    return {"ok": code2 == 0, "exit_code": code2, "output": output2, "target_version": target}
