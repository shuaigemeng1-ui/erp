"""离线自测（不依赖真实服务器 / 网络 / docker / systemd）。

用法::

    python3 erp_agent.py selftest
    python3 -c "from agentlib.selftest import run_selftest; raise SystemExit(run_selftest())"

覆盖：SQLite 执行日志与终态幂等、单实例单任务与 unknown 占用、结果先落库后补报、
心跳/续租/结果事件负载契约、完整 commit 校验、Agent 自升级版本切换与回滚、
指标缓存按时间顺序补报、上报退避策略。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from . import upgrade as upgrade_module
from . import versions as versions_module
from .constants import (
    AGENT_VERSION,
    JOB_LEASE_SECONDS,
    PROTOCOL_VERSION,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
)
from .httpc import CenterClient, NetworkError
from .jobs import JobRunner, report_backoff_seconds
from .journal import ActiveJobExists, Journal
from .telemetry import TelemetryCache, flush_cache


class FakeTransport:
    """可编排响应的假 transport：记录请求，按路径返回预设结果。"""

    def __init__(self, handlers: dict[str, Any] | None = None) -> None:
        self.handlers = handlers or {}
        self.calls: list[dict[str, Any]] = []

    def request(self, method, url, *, headers, body, timeout):  # noqa: ANN001
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "url": url, "payload": payload, "headers": headers})
        for suffix, response in self.handlers.items():
            if url.endswith(suffix):
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(payload)
                return response
        return 200, b'{"success": true, "data": {}}'


def _client(transport: FakeTransport) -> CenterClient:
    return CenterClient("https://center.invalid", "node-token", transport=transport)


def _check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    return bool(condition)


def _test_journal_basics(root: Path) -> bool:
    ok = True
    journal = Journal(root / "journal.db", lease_seconds=JOB_LEASE_SECONDS)
    journal.start_attempt(
        task_id="11",
        attempt_id="a1",
        job_type="restart",
        lease_token="lt-1",
        payload={"service": "api"},
    )
    ok &= _check("SQLite 记录租约", (journal.active_lease() or {}).get("task_id") == "11")
    try:
        journal.start_attempt(
            task_id="12", attempt_id="a2", job_type="backup", lease_token="lt-2"
        )
        ok &= _check("同一实例只允许一个活动任务", False, "第二个任务被错误接受")
    except ActiveJobExists:
        ok &= _check("同一实例只允许一个活动任务", True)
    journal.record_phase(
        task_id="11", attempt_id="a1", phase="step", step_no=1, step_name="重启 api",
        status=STATUS_RUNNING, message="执行中", command=["docker", "compose", "restart", "api"],
    )
    journal.record_subprocess(
        task_id="11", attempt_id="a1", phase="restart:api",
        command=["docker", "compose", "restart", "api"], pid=4321, exit_code=0, output_tail="ok",
    )
    ok &= _check("阶段与子进程日志落库", len(journal.list_phases("11")) == 1 and len(journal.list_subprocesses("11")) == 1)
    first = journal.persist_result(
        task_id="11", attempt_id="a1", status=STATUS_SUCCEEDED, result={"ok": True}, lease_token="lt-1"
    )
    second = journal.persist_result(
        task_id="11", attempt_id="a1", status=STATUS_FAILED, result={"ok": False}, lease_token="lt-1"
    )
    ok &= _check(
        "终态结果幂等（先写先得）",
        first.get("status") == STATUS_SUCCEEDED and second.get("status") == STATUS_SUCCEEDED,
    )
    journal.update_lease(task_id="11", attempt_id="a1", phase=STATUS_SUCCEEDED)
    ok &= _check("终态执行释放实例占用", journal.active_lease() is None)
    journal.close()
    return ok


def _test_unknown_occupancy(root: Path) -> bool:
    ok = True
    journal = Journal(root / "unknown.db")
    journal.start_attempt(task_id="21", attempt_id="b1", job_type="upgrade", lease_token="lt")
    journal.mark_unknown(task_id="21", attempt_id="b1", reason="执行器重启，结果未知")
    ok &= _check("unknown 继续占用实例", journal.has_unknown() and journal.active_lease() is not None)
    try:
        journal.start_attempt(task_id="22", attempt_id="b2", job_type="restart")
        ok &= _check("unknown 期间禁止领取新任务", False, "新任务被错误接受")
    except ActiveJobExists:
        ok &= _check("unknown 期间禁止领取新任务", True)
    journal.reconcile(task_id="21", note="人工确认升级完成")
    ok &= _check("reconcile 后解除占用", journal.active_lease() is None)
    journal.close()
    return ok


def _test_job_runner_flow(root: Path) -> bool:
    """完整跑一次：轮询 → 执行 → 先落库 → 上报失败保留 → 恢复后补发。"""
    ok = True
    transport = FakeTransport(
        {
            "/jobs/poll": (200, b'{"data": {"job": {"id": "31", "job_type": "backup", "payload": {}}}}'),
            "/jobs/31/events": NetworkError("center offline"),
        }
    )
    client = _client(transport)
    journal = Journal(root / "flow.db")
    executed: list[str] = []

    def executor(job, config, report, recorder):  # noqa: ANN001
        executed.append(str(job.get("id")))
        report(1, "执行备份", "running", "")
        recorder("backup", ["bash", "backup.sh"], 0, "BACKUP_KEY=k1", 1234)
        report(1, "执行备份", "succeeded", "完成")
        return {"ok": True, "exit_code": 0, "storage_key": "k1"}

    runner = JobRunner(client, journal, {}, executor=executor)
    result = runner.run_once()
    ok &= _check("执行器领取并执行任务", result.get("status") == STATUS_SUCCEEDED and executed == ["31"])
    persisted = journal.result_of("31")
    ok &= _check(
        "结果先落库（上报失败仍保留）",
        persisted is not None and persisted.get("status") == STATUS_SUCCEEDED
        and persisted.get("reported_at") is None,
    )
    ok &= _check("上报失败计入待补发", result.get("report", {}).get("failed") == 1)
    ok &= _check("无活动租约残留", journal.active_lease() is None)
    # 网络恢复：同一 transport 改为成功
    transport.handlers["/jobs/31/events"] = (200, b'{"success": true, "data": {"ok": true}}')
    summary = runner.flush_results(force=True)
    ok &= _check(
        "网络恢复后补发成功",
        summary.get("sent") == 1 and not journal.pending_results(ignore_backoff=True),
    )
    # 重复补发同一结果：保持幂等（不再有待发送）
    ok &= _check("终态补发幂等", runner.flush_results().get("sent") == 0)
    journal.close()
    return ok


def _test_protocol_payloads(root: Path) -> bool:
    ok = True
    transport = FakeTransport(
        {
            "/agent/heartbeat": (404, b'{"detail": "Not Found"}'),
            "/jobs/41/heartbeat": (404, b'{"detail": "Not Found"}'),
        }
    )
    client = _client(transport)
    from .heartbeat import HeartbeatSender, JobLeaseRenewer

    sender = HeartbeatSender(client, role="collector", state_provider=lambda: {"busy": False})
    payload = sender.payload()
    ok &= _check(
        "心跳负载包含协议/能力/版本",
        payload.get("protocol_version") == PROTOCOL_VERSION
        and payload.get("agent_version") == AGENT_VERSION
        and isinstance(payload.get("capabilities"), list),
    )
    result = sender.send_once()
    ok &= _check("新心跳接口 404 时降级而不崩溃", result.get("supported") is False)

    journal = Journal(root / "hb.db")
    journal.start_attempt(task_id="41", attempt_id="c1", job_type="restart", lease_token="lt")
    renewer = JobLeaseRenewer(client, journal, task_id="41", attempt_id="c1", lease_token="lt")
    renew = renewer.renew_once()
    ok &= _check("续租接口未实现时降级为不续租", renew.get("supported") is False)
    journal.close()
    return ok


def _test_commit_and_version_dirs(root: Path) -> bool:
    ok = True
    commit = "a" * 40
    try:
        versions_module.normalize_commit(commit[:12])
        ok &= _check("拒绝短 commit（必须完整 40/64 位）", False)
    except versions_module.VersionBuildError:
        ok &= _check("拒绝短 commit（必须完整 40/64 位）", True)
    ok &= _check("接受完整 40 位 commit", versions_module.normalize_commit(commit.upper()) == commit)
    ok &= _check("接受完整 64 位 commit", versions_module.normalize_commit("b" * 64) == "b" * 64)
    ok &= _check("拒绝分支名/非法值", _raises(versions_module.normalize_commit, "main"))

    config = {
        "repo_dir": str(root / "repo"),
        "deploy_dir": str(root / "repo" / "deploy"),
        "compose_project": "ventasync",
    }
    (Path(config["deploy_dir"])).mkdir(parents=True, exist_ok=True)
    fake = FakeRunner()
    fake.responses = {
        "rev-parse": (0, f"{commit}\n"),
        "cat-file": (0, ""),
        "worktree add": (0, "ok"),
    }
    target = versions_module.ensure_version_dir(fake, config, commit, fetch=False)
    ok &= _check("创建独立版本目录", target.is_dir() and (target / ".agent-version.json").is_file())
    ok &= _check(
        "版本目录使用 git worktree（不执行 reset --hard）",
        any("worktree" in " ".join(call[0]) for call in fake.calls)
        and not any("reset" in " ".join(call[0]) for call in fake.calls),
    )
    reused = versions_module.ensure_version_dir(fake, config, commit, fetch=False)
    ok &= _check("重复准备同一版本复用目录", reused == target)
    return ok


def _test_self_upgrade(root: Path) -> bool:
    ok = True
    agent_root = root / "agent-root"
    config_file = root / "config.json"
    config_file.write_text(json.dumps({"node_uuid": "uuid-1", "node_token": "secret"}), encoding="utf-8")
    source = root / "agent-src"
    (source / "agentlib").mkdir(parents=True)
    (source / "erp_agent.py").write_text("print('v2')\n", encoding="utf-8")
    (source / "executor.py").write_text("print('exec')\n", encoding="utf-8")
    (source / "agentlib" / "__init__.py").write_text("", encoding="utf-8")
    runner = FakeRunner(system_states={"erp-agent.service": "active", "erp-agent-executor.service": "active"})
    result = upgrade_module.self_upgrade(
        agent_root=agent_root,
        source=source,
        version="2.1.0",
        config_path=config_file,
        units=["erp-agent.service", "erp-agent-executor.service"],
        runner=runner,
        sleep=lambda _s: None,
    )
    ok &= _check("自升级切链并重启验证成功", result.ok, result.error)
    ok &= _check(
        "保留节点 UUID/config",
        json.loads(config_file.read_text(encoding="utf-8")).get("node_uuid") == "uuid-1",
    )
    current = upgrade_module.current_dir(agent_root)
    ok &= _check("current 指向新版本目录", current is not None and current.name == "2.1.0")

    # 重启验证失败 → 回滚到上一版本
    runner_fail = FakeRunner(
        system_states={"erp-agent.service": "failed", "erp-agent-executor.service": "active"}
    )
    result2 = upgrade_module.self_upgrade(
        agent_root=agent_root,
        source=source,
        version="2.2.0",
        config_path=config_file,
        units=["erp-agent.service", "erp-agent-executor.service"],
        runner=runner_fail,
        sleep=lambda _s: None,
        verify_timeout_seconds=1,
    )
    current_after = upgrade_module.current_dir(agent_root)
    ok &= _check(
        "重启验证失败自动回滚",
        (not result2.ok) and current_after is not None and current_after.name == "2.1.0",
        f"{result2.error} / current={current_after}",
    )
    ok &= _check(
        "回滚后配置仍保留",
        json.loads(config_file.read_text(encoding="utf-8")).get("node_token") == "secret",
    )
    return ok


def _test_telemetry_cache(root: Path) -> bool:
    ok = True
    cache = TelemetryCache(root / "cache.json", limit=5)
    sent: list[dict[str, Any]] = []

    def sender(item: dict[str, Any]) -> None:
        if item["sampled_at"] == "t2":
            raise NetworkError("offline")
        sent.append(item)

    items = [{"sampled_at": "t1"}, {"sampled_at": "t2"}, {"sampled_at": "t3"}]
    cache.write(items)
    summary = flush_cache(cache, sender, logger=lambda _m: None)
    ok &= _check("指标缓存遇到失败停止，保留后续（保持时间顺序）", summary["sent"] == 1 and summary["pending"] == 2)
    remaining = cache.read()
    ok &= _check("缓存保留失败及之后的样本", [item["sampled_at"] for item in remaining] == ["t2", "t3"])
    limited = TelemetryCache(root / "c2.json", limit=2)
    limited.write([{"sampled_at": "a"}, {"sampled_at": "b"}, {"sampled_at": "c"}])
    ok &= _check("缓存上限生效（保留最新）", [i["sampled_at"] for i in limited.read()] == ["b", "c"])
    ok &= _check("上报退避单调递增并封顶", report_backoff_seconds(1) == 5 and report_backoff_seconds(3) == 20 and report_backoff_seconds(20) == 300)
    return ok


class FakeRunner:
    """假命令执行器：记录调用、按关键字返回预设结果、可设定 systemctl 状态。"""

    def __init__(self, system_states: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.responses: dict[str, tuple[int, str]] = {}
        self.system_states = system_states or {}

    def run(self, args, cwd, *, timeout=300, env=None):  # noqa: ANN001
        command = [str(item) for item in args]
        self.calls.append((command, str(cwd)))
        if command[:2] == ["systemctl", "is-active"]:
            unit = command[2] if len(command) > 2 else ""
            state = self.system_states.get(unit, "active")
            return (0 if state == "active" else 3), state
        if command[:2] == ["systemctl", "restart"]:
            return 0, ""
        for marker, response in self.responses.items():
            if marker in " ".join(command):
                return response
        return 0, ""


def _raises(func, *args, **kwargs) -> bool:  # noqa: ANN001
    try:
        func(*args, **kwargs)
    except Exception:  # noqa: BLE001
        return True
    return False


def run_selftest() -> int:
    """运行全部离线自测，返回进程退出码。"""
    started = time.monotonic()
    print(f"Agent {AGENT_VERSION} 离线自测（协议 v{PROTOCOL_VERSION}，不依赖真实服务器）")
    ok = True
    with tempfile.TemporaryDirectory(prefix="erp-agent-selftest-") as temp:
        root = Path(temp)
        for name, func in (
            ("执行日志与终态幂等", _test_journal_basics),
            ("unknown 占用与 reconcile", _test_unknown_occupancy),
            ("任务执行→落库→补报", _test_job_runner_flow),
            ("心跳/续租负载与降级", _test_protocol_payloads),
            ("完整 commit 校验与版本目录", _test_commit_and_version_dirs),
            ("Agent 自升级与回滚", _test_self_upgrade),
            ("指标缓存补报与退避", _test_telemetry_cache),
        ):
            print(f"\n== {name} ==")
            try:
                ok = func(root) and ok
            except Exception as exc:  # noqa: BLE001 - 自测需要报告而不是崩溃
                print(f"[FAIL] {name} 抛出异常：{type(exc).__name__}: {exc}")
                ok = False
    elapsed = time.monotonic() - started
    print(f"\n自测{'通过' if ok else '失败'}，耗时 {elapsed:.2f}s")
    return 0 if ok else 1


__all__ = ["FakeRunner", "FakeTransport", "run_selftest"]
