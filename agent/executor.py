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
import shutil
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


def _compose(config: dict[str, Any], env_file_path: str | None = None) -> list[str]:
    deploy_dir = Path(str(config.get("deploy_dir") or "/opt/ventasync/deploy"))
    compose_file = str(config.get("compose_file") or "docker-compose.yml")
    compose_server = str(config.get("compose_server_file") or "docker-compose.server.yml")
    env_file = env_file_path or str(deploy_dir / str(config.get("env_file") or ".env"))
    return [
        "docker",
        "compose",
        "-f",
        str(deploy_dir / compose_file),
        "-f",
        str(deploy_dir / compose_server),
        "--env-file",
        env_file,
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
    # deploy / upgrade：目标版本写入临时 env 文件，固定镜像版本可本地执行；不修改正式 .env。
    target = str(job.get("target_version") or payload.get("target_version") or "").strip()
    if target and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", target):
        report(2, "校验版本", "failed", "版本号格式不正确")
        return {"ok": False, "error": "invalid_version"}
    env_source = Path(deploy_dir) / str(config.get("env_file") or ".env")
    temp_env = Path(f"/tmp/erp-agent-env-{os.getpid()}-{time.time_ns()}.env")
    try:
        lines = env_source.read_text(encoding="utf-8").splitlines()
        if target:
            lines = [line for line in lines if not line.startswith("APP_IMAGE_TAG=")]
            lines.append(f"APP_IMAGE_TAG={target}")
        temp_env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        compose = _compose(config, str(temp_env))
        has_registry = any(
            line.startswith("IMAGE_REGISTRY=") and line.split("=", 1)[1].strip()
            for line in lines
        )
        if target and has_registry:
            report(2, f"拉取镜像 {target}", "running", "")
            code, output = _run([*compose, "pull"], deploy_dir, timeout=1800)
            report(2, "拉取镜像", "succeeded" if code == 0 else "failed", output)
            if code != 0:
                return {"ok": False, "exit_code": code, "output": output}
        else:
            report(2, f"检查版本 {target or '当前版本'}", "succeeded", "")
        report(3, f"更新服务 {target or '当前版本'}", "running", "")
        code2, output2 = _run([*compose, "up", "-d", "--remove-orphans"], deploy_dir, timeout=1800)
        report(3, "更新服务", "succeeded" if code2 == 0 else "failed", output2)
        if code2 == 0 and target:
            try:
                # 升级成功后持久化版本标签，避免下一次普通 compose 启动回退到旧镜像。
                shutil.copyfile(temp_env, env_source)
            except OSError:
                pass
        time.sleep(3)
        return {"ok": code2 == 0, "exit_code": code2, "output": output2, "target_version": target}
    finally:
        try:
            temp_env.unlink()
        except OSError:
            pass
