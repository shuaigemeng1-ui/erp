"""应用升级：本地 git 版本目录构建（不再原地 ``git reset --hard``）。

流程（满足「目标版本完整 commit 校验」）：

1. 只接受完整 40/64 位十六进制 commit（短哈希直接拒绝，避免歧义）；
2. ``git rev-parse --verify <commit>^{commit}`` 复核对象就是该完整 commit；
3. ``git worktree add --detach`` 建独立版本目录（共享对象库，不污染主工作树，
   也不修改服务器仓库 HEAD，因此不会发生 ``reset --hard`` 丢改动）；
4. 在版本目录内 ``docker compose build``，用服务器原 ``deploy/.env`` 作为 env-file，
   compose 项目名固定，切换目录不会产生新栈；
5. ``up -d --remove-orphans`` 成功后写当前版本指针，并持久化镜像标签到 ``.env``；
6. 失败时保留原版本指针（回滚只需切回上一版本目录）。

所有外部命令都通过注入的 ``Runner`` 执行，便于自测（不依赖 docker / git 真实操作）。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

FULL_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
VERSION_FILE = ".agent-version.json"


class VersionBuildError(Exception):
    """版本目录构建 / 升级失败（可读中文原因）。"""


class Runner(Protocol):
    """命令执行协议：返回 ``(exit_code, output)``，输出需脱敏后返回。"""

    def run(
        self,
        args: Sequence[str],
        cwd: str | Path,
        *,
        timeout: int = 300,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]: ...


class SubprocessRunner:
    """默认 Runner：参数列表调用，禁止 shell；输出做基础脱敏。"""

    def __init__(self, redact: Callable[[str], str] | None = None) -> None:
        self._redact = redact or (lambda text: text)

    def run(
        self,
        args: Sequence[str],
        cwd: str | Path,
        *,
        timeout: int = 300,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        import os

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
            return int(result.returncode), self._redact(
                (result.stdout or "") + "\n" + (result.stderr or "")
            )
        except subprocess.TimeoutExpired:
            return 124, "命令执行超时"
        except OSError as exc:
            return 1, self._redact(str(exc))


def normalize_commit(value: str | None) -> str:
    """校验并返回小写完整 commit；不接受短哈希/分支名。"""
    text = (value or "").strip().lower()
    if not text:
        raise VersionBuildError("缺少目标版本 commit（必须提供完整 40/64 位 commit）")
    if not FULL_COMMIT_RE.match(text):
        raise VersionBuildError(f"目标版本 commit 非法：{value!r}（必须为完整 40/64 位十六进制）")
    return text


def versions_root(config: dict[str, Any]) -> Path:
    from . import config as config_module

    custom = str(config.get("app_versions_dir") or "").strip()
    if custom:
        return Path(custom)
    return config_module.repo_dir(config) / "versions"


def version_dir_for(config: dict[str, Any], commit: str) -> Path:
    return versions_root(config) / normalize_commit(commit)[:12]


def read_version_metadata(target: Path) -> dict[str, Any]:
    try:
        data = json.loads((target / VERSION_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_version_metadata(target: Path, payload: dict[str, Any]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / VERSION_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def verify_commit(runner: Runner, repo_dir: Path, commit: str) -> str:
    """复核完整 commit 存在且解析结果一致（防止短哈希歧义/伪造版本号）。"""
    normalized = normalize_commit(commit)
    code, output = runner.run(
        ["git", "rev-parse", "--verify", f"{normalized}^{{commit}}"], repo_dir, timeout=60
    )
    resolved = ""
    for line in (output or "").splitlines():
        candidate = line.strip().lower()
        if FULL_COMMIT_RE.match(candidate):
            resolved = candidate
            break
    if code != 0 or not resolved:
        raise VersionBuildError(f"git 中找不到目标 commit：{normalized}")
    if resolved != normalized:
        raise VersionBuildError(
            f"commit 校验不一致：请求 {normalized}，实际解析 {resolved}"
        )
    code, output = runner.run(
        ["git", "cat-file", "-e", f"{normalized}^{{commit}}"], repo_dir, timeout=60
    )
    if code != 0:
        raise VersionBuildError(f"目标 commit 对象不可用：{normalized}")
    return normalized


def fetch_origin(runner: Runner, repo_dir: Path, *, branch: str = "main") -> tuple[int, str]:
    """尽力更新远端引用；离线/无远端时返回失败但不直接中断（本地已有对象仍可用）。"""
    return runner.run(
        ["git", "fetch", "--prune", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
        repo_dir,
        timeout=600,
    )


def ensure_version_dir(
    runner: Runner,
    config: dict[str, Any],
    commit: str,
    *,
    fetch: bool = True,
    report: Callable[[str], None] | None = None,
) -> Path:
    """准备（必要时创建）独立 git 版本目录；已存在且 commit 一致时直接复用。"""
    from . import config as config_module

    log = report or (lambda message: None)
    normalized = normalize_commit(commit)
    repo = config_module.repo_dir(config)
    target = version_dir_for(config, normalized)
    metadata = read_version_metadata(target)
    if metadata.get("commit") == normalized:
        log(f"复用已有版本目录：{target}")
        return target
    if target.exists() and any(target.iterdir()):
        raise VersionBuildError(
            f"版本目录已存在但 commit 不匹配：{target}（请人工检查后移除）"
        )
    if fetch:
        code, output = fetch_origin(runner, repo)
        if code != 0:
            log(f"git fetch 失败，继续使用本地对象：{output.strip()[:200]}")
    verify_commit(runner, repo, normalized)
    target.parent.mkdir(parents=True, exist_ok=True)
    code, output = runner.run(
        ["git", "worktree", "add", "--detach", str(target), normalized], repo, timeout=600
    )
    if code != 0:
        raise VersionBuildError(f"创建版本目录失败：{output.strip()[:500]}")
    _write_version_metadata(
        target,
        {
            "commit": normalized,
            "short_commit": normalized[:12],
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": "git-worktree",
        },
    )
    log(f"已创建版本目录：{target}")
    return target


def compose_command(
    config: dict[str, Any],
    version_dir: Path,
    *,
    env_file: Path | None = None,
) -> list[str]:
    """版本目录内的 compose 命令：固定项目名 + 服务器现有 .env。"""
    from . import config as config_module

    deploy = version_dir / "deploy"
    compose_file = deploy / str(config.get("compose_file") or "docker-compose.yml")
    compose_server = deploy / str(config.get("compose_server_file") or "docker-compose.server.yml")
    env_path = env_file or config_module.deploy_dir(config) / str(config.get("env_file") or ".env")
    return [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "-f",
        str(compose_server),
        "--env-file",
        str(env_path),
        "-p",
        str(config.get("compose_project") or "ventasync"),
    ]


def compose_env(config: dict[str, Any], target_version: str) -> dict[str, str]:
    """构建/启动时覆盖的变量（不修改服务器 .env 也能指定本次版本镜像）。"""
    return {
        "APP_IMAGE_TAG": str(target_version),
        "APP_VERSION": str(target_version),
    }


def wait_services_ready(
    runner: Runner,
    compose: Sequence[str],
    services: Sequence[str] = ("api", "nginx"),
    *,
    timeout_seconds: int = 180,
    interval_seconds: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bool, str]:
    """等待关键服务进入 running/healthy；用于升级后的 readiness 校验。"""
    deadline = clock() + max(1, int(timeout_seconds))
    last = ""
    while clock() < deadline:
        code, output = runner.run([*compose, "ps", "--format", "json"], ".", timeout=60)
        last = output.strip()
        if code == 0:
            statuses = _parse_ps(output)
            if all(_service_ready(statuses.get(name)) for name in services):
                return True, last
        sleep(max(1, int(interval_seconds)))
    return False, last


def _service_ready(status: dict[str, Any] | None) -> bool:
    if not status:
        return False
    state = str(status.get("State") or status.get("state") or "").lower()
    health = str(status.get("Health") or status.get("health") or "").lower()
    return state == "running" and health in {"", "healthy", "starting"}


def _parse_ps(output: str) -> dict[str, dict[str, Any]]:
    """兼容 ``docker compose ps --format json`` 的数组与 NDJSON 两种输出。"""
    items: list[dict[str, Any]] = []
    text = (output or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            items = [item for item in parsed if isinstance(item, dict)]
        elif isinstance(parsed, dict):
            items = [parsed]
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                items.append(parsed)
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        name = str(item.get("Service") or item.get("service") or item.get("Name") or "")
        if name:
            result[name] = item
    return result


def persist_env_version(env_file: Path, target_version: str, *, image_registry: str = "") -> None:
    """把本次升级的镜像标签写入服务器 .env（保留其它配置行），供后续 restart 复用。"""
    keys = {"APP_IMAGE_TAG": target_version, "APP_VERSION": target_version, "IMAGE_REGISTRY": image_registry}
    lines: list[str] = []
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in keys]
    kept.extend(f"{key}={value}" for key, value in keys.items())
    env_file.parent.mkdir(parents=True, exist_ok=True)
    temp = env_file.with_name(env_file.name + ".agent-tmp")
    temp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    temp.replace(env_file)
    try:
        env_file.chmod(0o600)
    except OSError:
        pass


def switch_current(config: dict[str, Any], target: Path, commit: str) -> str:
    """记录当前版本指针（versions/current.json + current 软链），返回之前的 commit。"""
    root = versions_root(config)
    root.mkdir(parents=True, exist_ok=True)
    pointer = root / "current.json"
    previous = ""
    try:
        previous = str(json.loads(pointer.read_text(encoding="utf-8")).get("commit") or "")
    except (OSError, json.JSONDecodeError):
        previous = ""
    payload = {
        "commit": normalize_commit(commit),
        "dir": str(target),
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    pointer.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    link = root / "current"
    temp_link = root / ".current.tmp"
    try:
        if temp_link.exists() or temp_link.is_symlink():
            temp_link.unlink()
        temp_link.symlink_to(target)
        temp_link.replace(link)
    except OSError:
        # 文件系统不支持软链时以 current.json 为准（不影响升级判定）
        pass
    return previous


def current_commit(config: dict[str, Any]) -> str:
    try:
        data = json.loads((versions_root(config) / "current.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("commit") or "")


@dataclass
class UpgradeOutcome:
    ok: bool
    commit: str = ""
    version_dir: str = ""
    previous_commit: str = ""
    error: str = ""
    output: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "commit": self.commit,
            "version_dir": self.version_dir,
            "previous_commit": self.previous_commit,
            "error": self.error,
            "output": self.output[-4000:],
        }


def build_and_upgrade(
    runner: Runner,
    config: dict[str, Any],
    *,
    commit: str,
    target_version: str,
    report: Callable[[str], None] | None = None,
    build_timeout: int = 3600,
    up_timeout: int = 1800,
    ready_timeout: int = 180,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> UpgradeOutcome:
    """在独立版本目录内构建并启动目标 commit。"""
    from . import config as config_module

    log = report or (lambda message: None)
    previous = current_commit(config)
    try:
        normalized = normalize_commit(commit)
    except VersionBuildError as exc:
        return UpgradeOutcome(ok=False, error=str(exc), previous_commit=previous)
    try:
        version_dir = ensure_version_dir(runner, config, normalized, report=log)
    except VersionBuildError as exc:
        return UpgradeOutcome(ok=False, commit=normalized, error=str(exc), previous_commit=previous)

    compose = compose_command(config, version_dir)
    env = compose_env(config, target_version or normalized[:12])
    log(f"构建镜像（版本目录 {version_dir}）")
    code, output = runner.run([*compose, "build", "api", "nginx"], version_dir, timeout=build_timeout, env=env)
    if code != 0:
        return UpgradeOutcome(
            ok=False, commit=normalized, version_dir=str(version_dir),
            previous_commit=previous, error="镜像构建失败", output=output,
        )
    log("启动服务")
    code, output2 = runner.run(
        [*compose, "up", "-d", "--remove-orphans"], version_dir, timeout=up_timeout, env=env
    )
    combined = (output + "\n" + output2)[-6000:]
    if code != 0:
        return UpgradeOutcome(
            ok=False, commit=normalized, version_dir=str(version_dir),
            previous_commit=previous, error="服务启动失败", output=combined,
        )
    ready, ps_output = wait_services_ready(
        runner, compose, timeout_seconds=ready_timeout, sleep=sleep, clock=clock
    )
    if not ready:
        return UpgradeOutcome(
            ok=False, commit=normalized, version_dir=str(version_dir),
            previous_commit=previous, error="服务 readiness 校验未通过",
            output=(combined + "\n" + ps_output)[-6000:],
        )
    env_file = config_module.deploy_dir(config) / str(config.get("env_file") or ".env")
    try:
        persist_env_version(env_file, target_version or normalized[:12])
    except OSError as exc:
        log(f"写入 .env 版本标签失败（不阻塞升级）：{exc}")
    switch_current(config, version_dir, normalized)
    return UpgradeOutcome(
        ok=True, commit=normalized, version_dir=str(version_dir),
        previous_commit=previous, output=combined,
    )


__all__ = [
    "FULL_COMMIT_RE",
    "Runner",
    "SubprocessRunner",
    "UpgradeOutcome",
    "VersionBuildError",
    "build_and_upgrade",
    "compose_command",
    "current_commit",
    "ensure_version_dir",
    "normalize_commit",
    "persist_env_version",
    "read_version_metadata",
    "verify_commit",
    "version_dir_for",
    "versions_root",
    "wait_services_ready",
]
