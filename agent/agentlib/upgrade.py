"""Agent 自身升级 helper（独立版本目录切换 + systemd 重启验证 + 回滚）。

安装布局::

    /opt/erp-agent/
      versions/
        2.0.0/            # 每个 Agent 版本一个独立目录（erp_agent.py/executor.py/agentlib）
        2.1.0/
        current -> versions/2.0.0
      install.json        # 安装元数据（不随版本切换变化）

约束（对应任务要求）：

* 只切换 ``current`` 软链，**不覆盖运行中的代码目录**，切换是原子的；
* ``/etc/erp-agent/config.json`` 在版本目录之外，升级不会触碰节点 UUID / 节点令牌 / 部署路径；
* 切换后重启两个 systemd unit 并验证 ``is-active``，失败自动切回上一版本并再重启；
* helper 自身也可被旧版本调用（独立模块、无新依赖），符合「旧 Agent 也能自升级」。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .versions import normalize_commit

REQUIRED_ENTRIES = ("erp_agent.py", "executor.py", "agentlib/__init__.py")
INSTALL_FILE = "install.json"


class UpgradeError(Exception):
    """Agent 自升级失败（可读中文原因）。"""


class CommandRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        cwd: str | Path,
        *,
        timeout: int = 120,
    ) -> tuple[int, str]: ...


@dataclass
class UpgradeResult:
    ok: bool
    version: str = ""
    target_dir: str = ""
    previous_dir: str = ""
    error: str = ""
    config_sha256: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "version": self.version,
            "target_dir": self.target_dir,
            "previous_dir": self.previous_dir,
            "error": self.error,
            "config_sha256": self.config_sha256,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def normalize_version(value: str) -> str:
    text = (value or "").strip()
    if not text or "/" in text or ".." in text or len(text) > 64:
        raise UpgradeError(f"Agent 版本号非法：{value!r}")
    if not all(char.isalnum() or char in "._-" for char in text):
        raise UpgradeError(f"Agent 版本号包含非法字符：{value!r}")
    return text


def _versions_dir(agent_root: Path) -> Path:
    return Path(agent_root) / "versions"


def current_dir(agent_root: Path) -> Path | None:
    """当前生效版本目录（优先软链；不支持软链时读 install.json）。"""
    root = Path(agent_root)
    link = root / "current"
    if link.is_symlink():
        try:
            return link.resolve()
        except OSError:
            return None
    meta = read_install_meta(root)
    value = str(meta.get("current_dir") or "")
    return Path(value) if value else None


def read_install_meta(agent_root: Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(agent_root) / INSTALL_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_install_meta(agent_root: Path, payload: dict[str, Any]) -> None:
    root = Path(agent_root)
    root.mkdir(parents=True, exist_ok=True)
    temp = root / f".{INSTALL_FILE}.tmp"
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(root / INSTALL_FILE)


def _extract_tarball(archive: Path, target: Path) -> None:
    """安全解包：拒绝绝对路径与 ``..``，只解到目标版本目录。"""
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise UpgradeError(f"压缩包包含非法路径：{name}")
        tar.extractall(str(target))
    # 常见打包方式会带一层顶层目录，自动下钻到真实代码根
    entries = list(target.iterdir())
    if len(entries) == 1 and entries[0].is_dir() and not (target / "erp_agent.py").exists():
        inner = entries[0]
        for item in inner.iterdir():
            shutil.move(str(item), str(target / item.name))
        inner.rmdir()


def verify_version_dir(target: Path) -> None:
    missing = [entry for entry in REQUIRED_ENTRIES if not (target / entry).exists()]
    if missing:
        raise UpgradeError(f"版本目录缺少必要文件：{', '.join(missing)}")


def stage_version(
    source: str | Path,
    agent_root: str | Path,
    version: str,
    *,
    commit: str = "",
    force: bool = False,
) -> Path:
    """把源码目录/离线包复制到 ``versions/<version>``（已存在时默认复用并校验）。"""
    version_value = normalize_version(version)
    if commit:
        commit = normalize_commit(commit)  # 完整 commit 校验，短哈希直接拒绝
    root = Path(agent_root)
    target = _versions_dir(root) / version_value
    if target.exists() and any(target.iterdir()) and not force:
        verify_version_dir(target)
        return target
    if target.exists() and force:
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    src = Path(source)
    if not src.exists():
        raise UpgradeError(f"升级源不存在：{src}")
    if src.is_dir():
        for entry in ("erp_agent.py", "executor.py", "agentlib"):
            if not (src / entry).exists():
                raise UpgradeError(f"升级源缺少 {entry}")
        for entry in ("erp_agent.py", "executor.py"):
            shutil.copy2(src / entry, target / entry)
        shutil.copytree(src / "agentlib", target / "agentlib", dirs_exist_ok=True)
        for name in ("README.md", "PROTOCOL.md"):
            if (src / name).exists():
                shutil.copy2(src / name, target / name)
    elif src.is_file() and src.suffix in {".gz", ".tgz"}:
        _extract_tarball(src, target)
    else:
        raise UpgradeError("升级源必须是 Agent 源码目录或 tar.gz 离线包")
    verify_version_dir(target)
    meta = {
        "version": version_value,
        "commit": commit,
        "staged_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    (target / ".agent-self.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        os.chmod(target / "erp_agent.py", 0o755)
    except OSError:
        pass
    return target


def switch_current(agent_root: str | Path, target: Path, *, dry_run: bool = False) -> Path | None:
    """原子切换 ``current`` 软链，返回切换前目录。"""
    root = Path(agent_root)
    root.mkdir(parents=True, exist_ok=True)
    previous = current_dir(root)
    if dry_run:
        return previous
    link = root / "current"
    temp_link = root / ".current.tmp"
    try:
        if temp_link.exists() or temp_link.is_symlink():
            temp_link.unlink()
        temp_link.symlink_to(Path(target))
        temp_link.replace(link)
    except OSError as exc:
        # 文件系统不支持软链：退回 install.json 记录，行为等价
        meta = read_install_meta(root)
        meta["current_dir"] = str(target)
        meta["current_version"] = normalize_version(Path(target).name)
        write_install_meta(root, meta)
        if not (root / "current").exists():
            return previous
        raise UpgradeError(f"切换 current 软链失败：{exc}") from exc
    return previous


def restart_units(
    runner: CommandRunner,
    units: Sequence[str],
    *,
    timeout: int = 180,
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    log = logger or (lambda message: None)
    outputs: dict[str, Any] = {}
    for unit in units:
        code, output = runner.run(["systemctl", "restart", unit], ".", timeout=timeout)
        outputs[unit] = {"code": code, "output": output[-1000:]}
        log(f"重启 {unit} → exit={code}")
        if code != 0:
            raise UpgradeError(f"重启 {unit} 失败：{output.strip()[:300]}")
    return outputs


def verify_units(
    runner: CommandRunner,
    units: Sequence[str],
    *,
    timeout_seconds: int = 90,
    interval_seconds: int = 2,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """轮询 ``systemctl is-active``，全部 active 才算重启验证通过。"""
    deadline = clock() + max(1, int(timeout_seconds))
    states: dict[str, str] = {}
    while clock() < deadline:
        all_active = True
        for unit in units:
            code, output = runner.run(["systemctl", "is-active", unit], ".", timeout=30)
            state = (output or "").strip().splitlines()[-1] if (output or "").strip() else ""
            states[unit] = state or f"exit={code}"
            if state != "active":
                all_active = False
        if all_active:
            return {"ok": True, "states": states}
        sleep(max(1, int(interval_seconds)))
    return {"ok": False, "states": states}


def self_upgrade(
    *,
    agent_root: str | Path,
    source: str | Path,
    version: str,
    config_path: str | Path,
    units: Sequence[str],
    commit: str = "",
    runner: CommandRunner,
    restart: bool = True,
    dry_run: bool = False,
    verify_timeout_seconds: int = 90,
    logger: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> UpgradeResult:
    """完整自升级流程：暂存 → 校验 config → 切链 → 重启 → 验证，失败回滚。"""
    log = logger or (lambda message: None)
    config_file = Path(config_path)
    config_hash_before = sha256_file(config_file)
    result = UpgradeResult(ok=False, config_sha256=config_hash_before)
    try:
        target = stage_version(source, agent_root, version, commit=commit)
        result.version = normalize_version(version)
        result.target_dir = str(target)
    except (UpgradeError, ValueError) as exc:
        result.error = str(exc)
        return result

    previous = switch_current(agent_root, target, dry_run=dry_run)
    result.previous_dir = str(previous) if previous else ""
    if dry_run:
        result.ok = True
        return result

    meta = read_install_meta(Path(agent_root))
    meta.update(
        {
            "current_dir": str(target),
            "current_version": result.version,
            "previous_dir": result.previous_dir,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )
    write_install_meta(Path(agent_root), meta)

    try:
        if restart:
            restart_units(runner, units, logger=log)
            verification = verify_units(
                runner, units, timeout_seconds=verify_timeout_seconds, sleep=sleep, clock=clock
            )
            if not verification.get("ok"):
                raise UpgradeError(f"重启验证未通过：{verification.get('states')}")
    except (UpgradeError, OSError) as exc:
        log(f"升级失败，回滚到上一版本：{exc}")
        result.error = str(exc)
        if previous is not None:
            try:
                switch_current(agent_root, previous)
                if restart:
                    restart_units(runner, units, logger=log)
                    verify_units(runner, units, timeout_seconds=verify_timeout_seconds, sleep=sleep)
            except (UpgradeError, OSError) as rollback_exc:  # pragma: no cover - 极端情况
                log(f"回滚失败，需人工介入：{rollback_exc}")
                result.error = f"{exc}；回滚失败：{rollback_exc}"
        else:
            result.error = f"{exc}；没有上一版本可回滚"
        return result

    # 配置（节点 UUID / 令牌）必须与升级前完全一致
    config_hash_after = sha256_file(config_file)
    result.config_sha256 = config_hash_after
    if config_hash_before and config_hash_after and config_hash_before != config_hash_after:
        result.error = "升级过程中配置文件被修改（节点身份不保），请人工检查"
        return result
    result.ok = True
    log(f"Agent 已切换到版本 {result.version}（{target}）")
    return result


__all__ = [
    "INSTALL_FILE",
    "REQUIRED_ENTRIES",
    "CommandRunner",
    "UpgradeError",
    "UpgradeResult",
    "current_dir",
    "normalize_version",
    "read_install_meta",
    "restart_units",
    "self_upgrade",
    "sha256_file",
    "stage_version",
    "switch_current",
    "verify_units",
    "verify_version_dir",
    "write_install_meta",
]
