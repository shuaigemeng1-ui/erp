#!/usr/bin/env python3
"""ERP Agent 本机受限执行器（Agent 2）。

只接受管理中心下发的固定任务类型：

* ``restart``：重启指定 compose 服务（api/worker/scheduler/nginx）
* ``backup``：执行部署目录内固定的 backup.sh
* ``logs``：读取指定 compose 服务最近日志并脱敏
* ``deploy``：按当前版本启动 compose 项目（不做 git 操作）
* ``upgrade``：**必须携带完整 40/64 位 commit**，在本地 git 版本目录内构建并启动，
  不再原地 ``git reset --hard``；构建/就绪校验通过后才切换当前版本指针。

不执行任意 Shell，不接受用户拼接命令；所有命令使用参数列表调用。
外部命令通过可注入的 ``Runner`` 执行，``recorder`` 回调把每个子进程写入本地 SQLite，
便于事后核对。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from agentlib import versions
from agentlib.constants import ALLOWED_JOB_TYPES

ALLOWED_SERVICES = {"api", "worker", "scheduler", "nginx"}
ALLOWED_ACTIONS = set(ALLOWED_JOB_TYPES)
SECRET_PATTERNS = [
    re.compile(r"(?i)(password|passwd|secret|token|access[_-]?key|secret[_-]?key)\s*[=:]\s*\S+"),
    re.compile(r"(?i)(authorization:\s*bearer)\s+\S+"),
]


def redact(text: str) -> str:
    value = text or ""
    for pattern in SECRET_PATTERNS:
        value = pattern.sub(lambda m: f"{m.group(1)}=***", value)
    return value[-4000:]


class CommandRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        cwd: str | Path,
        *,
        timeout: int = 300,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]: ...


class SubprocessCommandRunner:
    """默认命令执行器：参数列表 + 超时 + 环境变量覆盖 + 输出脱敏。"""

    def run(
        self,
        args: Sequence[str],
        cwd: str | Path,
        *,
        timeout: int = 300,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        try:
            result = subprocess.run(
                list(args),
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=int(timeout),
                check=False,
                env={**os.environ, "COMPOSE_INTERACTIVE_NO_CLI": "1", **(env or {})},
            )
            return int(result.returncode), redact((result.stdout or "") + "\n" + (result.stderr or ""))
        except subprocess.TimeoutExpired:
            return 124, "任务执行超时"
        except OSError as exc:
            return 1, redact(str(exc))


def _default_recorder(*_args: Any, **_kwargs: Any) -> None:
    return None


class Executor:
    """受限执行器：把 job 映射为固定命令，全部通过注入的 runner 执行。"""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner: CommandRunner = runner or SubprocessCommandRunner()

    # ------------------------------------------------------------------ 基础
    def _run(
        self,
        command: list[str],
        cwd: str | Path,
        *,
        phase: str,
        recorder: Callable[..., None],
        timeout: int = 300,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        recorder(phase, command, None, "", None)
        code, output = self.runner.run(command, cwd, timeout=timeout, env=env)
        recorder(phase, command, code, output, None)
        return code, output

    def _compose(self, config: dict[str, Any], env_file_path: str | None = None) -> list[str]:
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
            "-p",
            str(config.get("compose_project") or "ventasync"),
        ]

    # ------------------------------------------------------------------ 入口
    def execute(
        self,
        job: dict[str, Any],
        config: dict[str, Any],
        report: Callable[..., None],
        recorder: Callable[..., None] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        """执行一个任务；返回 ``{ok, exit_code, output, ...}``（output 只留本地）。"""
        record = recorder or _default_recorder
        job_type = str(job.get("job_type") or "")
        payload = job.get("payload") or {}
        if job_type not in ALLOWED_ACTIONS:
            report(1, "校验任务", "failed", f"不支持的任务类型：{job_type}")
            return {"ok": False, "error": "unsupported_job_type"}
        deploy_dir = str(Path(str(config.get("deploy_dir") or "/opt/ventasync/deploy")))
        compose = self._compose(config)
        report(1, "检查任务参数", "succeeded", f"任务类型：{job_type}")

        if job_type == "restart":
            return self._restart(payload, deploy_dir, compose, report, record)
        if job_type == "logs":
            return self._logs(payload, deploy_dir, compose, report, record)
        if job_type == "backup":
            return self._backup(deploy_dir, report, record)
        if job_type == "deploy":
            return self._deploy(job, payload, deploy_dir, compose, report, record)
        return self._upgrade(job, payload, config, report, record, sleep=sleep, clock=clock)

    # ------------------------------------------------------------------ 各任务
    def _restart(self, payload: dict[str, Any], deploy_dir: str, compose: list[str], report, record) -> dict[str, Any]:
        service = str(payload.get("service") or "api")
        if service not in ALLOWED_SERVICES:
            report(2, "校验服务", "failed", "服务不在允许列表")
            return {"ok": False, "error": "service_not_allowed"}
        report(2, f"重启 {service}", "running", "")
        code, output = self._run(
            [*compose, "restart", service], deploy_dir, phase=f"restart:{service}",
            recorder=record, timeout=180,
        )
        report(2, f"重启 {service}", "succeeded" if code == 0 else "failed", output)
        return {"ok": code == 0, "exit_code": code, "output": output}

    def _logs(self, payload: dict[str, Any], deploy_dir: str, compose: list[str], report, record) -> dict[str, Any]:
        service = str(payload.get("service") or "api")
        if service not in ALLOWED_SERVICES:
            report(2, "校验服务", "failed", "服务不在允许列表")
            return {"ok": False, "error": "service_not_allowed"}
        try:
            tail = max(10, min(int(payload.get("tail") or 200), 500))
        except (TypeError, ValueError):
            tail = 200
        code, output = self._run(
            [*compose, "logs", "--tail", str(tail), service], deploy_dir,
            phase=f"logs:{service}", recorder=record, timeout=60,
        )
        report(2, f"读取 {service} 日志", "succeeded" if code == 0 else "failed", output)
        return {"ok": code == 0, "exit_code": code, "output": output}

    def _backup(self, deploy_dir: str, report, record) -> dict[str, Any]:
        script = Path(deploy_dir) / "backup.sh"
        if not script.exists():
            report(2, "执行备份", "failed", "backup.sh 不存在")
            return {"ok": False, "error": "backup_script_missing"}
        report(2, "执行备份", "running", "")
        code, output = self._run(
            ["bash", str(script)], deploy_dir, phase="backup", recorder=record, timeout=3600
        )
        key = ""
        size = 0
        match = re.search(r"BACKUP_KEY=(\S+)", output)
        if match:
            key = match.group(1)
        size_match = re.search(r"BACKUP_SIZE=(\d+)", output)
        if size_match:
            size = int(size_match.group(1))
        report(2, "执行备份", "succeeded" if code == 0 else "failed", output)
        return {
            "ok": code == 0,
            "exit_code": code,
            "output": output,
            "storage_key": key,
            "size_bytes": size,
        }

    def _deploy(
        self,
        job: dict[str, Any],
        payload: dict[str, Any],
        deploy_dir: str,
        compose: list[str],
        report,
        record,
    ) -> dict[str, Any]:
        """按当前版本启动服务；target_version 只覆盖镜像标签，不做 git 变更。"""
        target = str(job.get("target_version") or payload.get("target_version") or "").strip()
        if target and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", target):
            report(2, "校验版本", "failed", "版本号格式不正确")
            return {"ok": False, "error": "invalid_version"}
        env = {"APP_IMAGE_TAG": target, "APP_VERSION": target} if target else None
        report(2, "启动服务", "running", "")
        code, output = self._run(
            [*compose, "up", "-d", "--remove-orphans"], deploy_dir,
            phase="deploy:up", recorder=record, timeout=1800, env=env,
        )
        report(2, "启动服务", "succeeded" if code == 0 else "failed", output)
        return {"ok": code == 0, "exit_code": code, "output": output, "target_version": target}

    def _upgrade(
        self,
        job: dict[str, Any],
        payload: dict[str, Any],
        config: dict[str, Any],
        report,
        record,
        *,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> dict[str, Any]:
        """在独立 git 版本目录内构建并启动目标完整 commit。"""
        raw_commit = str(
            payload.get("commit") or payload.get("target_commit") or job.get("commit") or ""
        ).strip()
        target_version = str(
            job.get("target_version") or payload.get("target_version") or raw_commit[:12]
        ).strip()
        report(2, "校验目标版本", "running", f"commit={raw_commit or '(空)'}")
        try:
            commit = versions.normalize_commit(raw_commit)
        except versions.VersionBuildError as exc:
            report(2, "校验目标版本", "failed", str(exc))
            return {"ok": False, "error": "invalid_commit", "detail": str(exc)}
        report(2, "校验目标版本", "succeeded", f"完整 commit={commit}")

        report(3, "准备版本目录", "running", "")
        try:
            version_dir = versions.ensure_version_dir(
                self.runner,
                config,
                commit,
                report=lambda message: report(3, "准备版本目录", "running", message),
            )
        except versions.VersionBuildError as exc:
            report(3, "准备版本目录", "failed", str(exc))
            return {"ok": False, "error": "version_dir_failed", "detail": str(exc)}
        report(3, "准备版本目录", "succeeded", str(version_dir))

        compose = versions.compose_command(config, version_dir)
        env = versions.compose_env(config, target_version)
        report(4, "构建镜像", "running", f"目录={version_dir}")
        code, output = self._run(
            [*compose, "build", "api", "nginx"], version_dir,
            phase="upgrade:build", recorder=record, timeout=3600, env=env,
        )
        if code != 0:
            report(4, "构建镜像", "failed", output)
            return {"ok": False, "exit_code": code, "output": output, "error": "build_failed"}

        report(4, "构建镜像", "succeeded", "")
        report(5, "启动服务", "running", f"版本={target_version}")
        code, output2 = self._run(
            [*compose, "up", "-d", "--remove-orphans"], version_dir,
            phase="upgrade:up", recorder=record, timeout=1800, env=env,
        )
        combined = (output + "\n" + output2)[-6000:]
        if code != 0:
            report(5, "启动服务", "failed", output2)
            return {"ok": False, "exit_code": code, "output": combined, "error": "up_failed"}

        report(6, "服务就绪校验", "running", "等待 api/nginx 进入 running")
        ready, ps_output = versions.wait_services_ready(
            self.runner, compose, timeout_seconds=180, sleep=sleep, clock=clock
        )
        if not ready:
            report(6, "服务就绪校验", "failed", ps_output[-1000:])
            return {
                "ok": False,
                "output": (combined + "\n" + ps_output)[-6000:],
                "error": "readiness_failed",
            }
        report(6, "服务就绪校验", "succeeded", "")

        env_file = Path(str(config.get("deploy_dir") or "/opt/ventasync/deploy")) / str(
            config.get("env_file") or ".env"
        )
        try:
            versions.persist_env_version(env_file, target_version or commit[:12])
        except OSError:
            pass
        previous = versions.switch_current(config, version_dir, commit)
        report(7, "切换当前版本", "succeeded", f"{previous or '(无)'} → {commit}")
        return {
            "ok": True,
            "exit_code": 0,
            "output": combined,
            "commit": commit,
            "target_version": target_version,
            "version_dir": str(version_dir),
            "previous_commit": previous,
            "db_version": str(payload.get("db_version") or ""),
            "checks": {
                "commit_verified": True,
                "build": "succeeded",
                "services_ready": "succeeded",
            },
        }


_DEFAULT = Executor()


def execute(
    job: dict[str, Any],
    config: dict[str, Any],
    report: Callable[..., None],
    recorder: Callable[..., None] | None = None,
    *,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """兼容旧调用方式的模块级入口（Agent 2 内部由 ``Executor`` 实例执行）。"""
    executor = Executor(runner) if runner is not None else _DEFAULT
    return executor.execute(job, config, report, recorder)


__all__ = [
    "ALLOWED_ACTIONS",
    "ALLOWED_SERVICES",
    "CommandRunner",
    "Executor",
    "SubprocessCommandRunner",
    "execute",
    "redact",
]
