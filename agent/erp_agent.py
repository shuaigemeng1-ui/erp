#!/usr/bin/env python3
"""ERP 租户服务器 Agent 2 入口（仅标准库，Linux）。

角色分离（部署为两个 systemd unit）：

* ``run``（采集进程）：指标采集 + 30s 心跳 + 断网缓存补报 + 结果补报；
  只读采集，不执行任何变更任务。
* ``executor``（执行进程）：轮询任务、持有/续租 120s 租约、受限执行、
  本地 SQLite 执行日志、结果先落库后上报；权限更高，与采集进程分离。

常用命令::

    # 注册（安装时执行一次）
    sudo python3 erp_agent.py enroll --center https://erp.example.com \
        --token <一次性令牌> --instance 1 --config /etc/erp-agent/config.json

    # 采集进程（systemd: erp-agent.service）
    python3 erp_agent.py run --config /etc/erp-agent/config.json

    # 执行进程（systemd: erp-agent-executor.service）
    python3 erp_agent.py executor --config /etc/erp-agent/config.json

    # 状态 / 人工核对 / 自升级 / 自测
    python3 erp_agent.py status --config /etc/erp-agent/config.json
    python3 erp_agent.py reconcile --job-id 12 --note "人工确认已完成"
    python3 erp_agent.py self-upgrade --source /opt/ventasync/agent --version 2.1.0
    python3 erp_agent.py selftest
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import signal
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# 允许以脚本方式运行（systemd ExecStart 指向 current/erp_agent.py）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentlib import config as config_module  # noqa: E402
from agentlib import upgrade as upgrade_module  # noqa: E402
from agentlib.constants import (  # noqa: E402
    AGENT_VERSION,
    CAPABILITIES,
    EXECUTOR_VERSION,
    HEARTBEAT_SECONDS,
    JOB_LEASE_RENEW_SECONDS,
    JOB_LEASE_SECONDS,
    PROTOCOL_VERSION,
    RESULT_REPORT_SECONDS,
    TELEMETRY_SECONDS,
)
from agentlib.heartbeat import HeartbeatSender  # noqa: E402
from agentlib.httpc import CenterClient, data_of  # noqa: E402
from agentlib.jobs import JobRunner  # noqa: E402
from agentlib.journal import Journal  # noqa: E402
from agentlib.telemetry import (  # noqa: E402
    TelemetryCache,
    TelemetryReporter,
    collect,
    detect_app_version,
)

import executor as executor_module  # noqa: E402  （受限执行器，版本目录内同目录导入）

LOGGER = logging.getLogger("erp-agent")
_STOP = threading.Event()


# --------------------------------------------------------------------------- 兼容旧接口
def load_config(path: str) -> dict[str, Any]:
    """兼容旧版调用：读取配置（补齐默认值）。"""
    return config_module.load_config(path)


def save_config(path: str, config: dict[str, Any]) -> None:
    """兼容旧版调用：原子保存配置（0600）。"""
    config_module.save_config(path, config)


def http_json(url: str, payload: dict[str, Any] | None, token: str = "", timeout: int = 10) -> dict:
    """兼容旧版调用：单次 JSON 请求（内部改用 CenterClient）。"""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}" if parts.scheme else url.rsplit("/api/v1", 1)[0]
    client = CenterClient(base, token, timeout=timeout)
    path = parts.path or "/"
    if payload is None:
        return client.get(path)
    return client.post(path, payload)


# --------------------------------------------------------------------------- 工具
def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
    )


def log(message: str) -> None:
    LOGGER.info(message)


def build_client(config: dict[str, Any]) -> CenterClient:
    return CenterClient(
        config_module.center_url(config),
        config_module.node_token(config),
        logger=log,
    )


def open_journal(config: dict[str, Any]) -> Journal:
    config_module.ensure_state_dir(config)
    return Journal(
        config_module.journal_path(config),
        lease_seconds=int(config.get("job_lease_seconds") or JOB_LEASE_SECONDS),
    )


def _install_signal_handlers() -> None:
    def handler(signum: int, _frame: Any) -> None:
        log(f"收到信号 {signum}，准备退出")
        _STOP.set()

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - 非主线程安装信号
                pass


def _config_view(config: dict[str, Any]) -> dict[str, Any]:
    """打印/上报用配置视图：绝不输出节点令牌明文。"""
    view = {
        key: value
        for key, value in config.items()
        if key not in {"node_token", "node_uuid"}
    }
    token = str(config.get("node_token") or "")
    view["node_token"] = f"***{token[-4:]}" if token else ""
    view["node_uuid"] = str(config.get("node_uuid") or "")
    return view


# --------------------------------------------------------------------------- enroll
def cmd_enroll(args: argparse.Namespace) -> int:
    base = str(args.center).rstrip("/")
    client = CenterClient(base, "", logger=log)
    metrics = collect(config=None)
    payload = {
        "token": args.token,
        "hostname": metrics["hostname"],
        "os_name": metrics["os_name"],
        "arch": metrics["arch"],
        "cpu_cores": metrics["cpu_cores"],
        "memory_total_mb": metrics["memory_total_mb"],
        "disk_total_gb": metrics["disk_total_gb"],
        "internal_ips": metrics["internal_ips"],
        "public_ip": metrics["public_ip"],
        "agent_version": AGENT_VERSION,
        "app_version": metrics["app_version"],
        # Agent 2 新增：协议版本与能力，后端可据此判断是否下发变更任务
        "agent_protocol_version": PROTOCOL_VERSION,
        "executor_version": EXECUTOR_VERSION,
        "capabilities": list(CAPABILITIES),
        "metadata": {
            "instance_id": args.instance,
            "python": platform.python_version(),
            "agent_version": AGENT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
        },
    }
    result = client.enroll(payload)
    data = data_of(result)
    token = data.get("node_token")
    if not token:
        print(f"注册失败：响应缺少 node_token：{json.dumps(result, ensure_ascii=False)[:500]}")
        return 1
    existing = config_module.load_config(args.config)
    existing.update(
        {
            "center": base,
            "node_id": data.get("node_id"),
            "node_uuid": data.get("node_uuid"),
            "node_token": token,
            "instance_id": data.get("instance_id") or args.instance,
            "agent_version": AGENT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
        }
    )
    config_module.save_config(args.config, existing)
    config_module.ensure_state_dir(existing)
    print(f"注册成功：node_id={data.get('node_id')} node_uuid={data.get('node_uuid')}")
    print(f"配置已保存：{args.config}（权限 600，Agent 升级不会修改该文件）")
    return 0


# --------------------------------------------------------------------------- 采集进程
def cmd_run(args: argparse.Namespace) -> int:
    config = config_module.load_config(args.config)
    if not config_module.center_url(config):
        print("配置缺少 center，请先执行 enroll", file=sys.stderr)
        return 2
    if str(args.role) == "executor":
        return cmd_executor(args)
    _install_signal_handlers()
    client = build_client(config)
    journal = open_journal(config)
    cache = TelemetryCache(config_module.telemetry_cache_path(config))
    boot_id = uuid.uuid4().hex
    reporter = TelemetryReporter(client, cache, boot_id=boot_id, logger=log)
    previous_network: dict[str, Any] | None = None
    telemetry_seconds = int(config.get("telemetry_seconds") or TELEMETRY_SECONDS)
    heartbeat_seconds = int(config.get("heartbeat_seconds") or HEARTBEAT_SECONDS)
    report_seconds = int(config.get("result_report_seconds") or RESULT_REPORT_SECONDS)

    result_reporter = JobRunner(client, journal, config, executor=lambda *a, **k: {}, logger=log)
    heartbeat = HeartbeatSender(
        client,
        role="collector",
        config=config,
        boot_id=boot_id,
        state_provider=lambda: {
            "telemetry": {"queued": reporter.queued},
            "executor": result_reporter.status(),
            "journal": journal.status_summary()["counts"],
            "agent_protocol_version": PROTOCOL_VERSION,
        },
        logger=log,
        interval=heartbeat_seconds,
    )
    heartbeat_thread = threading.Thread(
        target=heartbeat.run_forever,
        args=(_STOP,),
        name="agent-heartbeat",
        daemon=True,
    )

    def replay_results() -> None:
        try:
            summary = result_reporter.flush_results()
            if summary.get("sent") or summary.get("failed"):
                log(f"结果补报：{summary}")
        except Exception as exc:  # noqa: BLE001 - 补报失败不影响采集
            log(f"结果补报异常：{exc}")

    def report_thread() -> None:
        while not _STOP.wait(max(5, report_seconds)):
            replay_results()

    log(
        f"Agent {AGENT_VERSION} 采集进程启动：center={config_module.center_url(config)} "
        f"node_id={config.get('node_id')} 采集间隔={telemetry_seconds}s"
    )
    heartbeat_thread.start()
    threading.Thread(target=report_thread, name="result-reporter", daemon=True).start()
    if str(args.role) == "all":
        executor_thread = threading.Thread(
            target=_executor_loop, args=(config,), name="executor-inline", daemon=True
        )
        executor_thread.start()

    while not _STOP.is_set():
        tick_started = time.monotonic()
        try:
            payload = collect(previous_network, config=config, report_seconds=telemetry_seconds)
            payload["app_version"] = detect_app_version(config)
            payload["agent_protocol_version"] = PROTOCOL_VERSION
            payload["executor_version"] = EXECUTOR_VERSION
            previous_network = payload["network"]
            reporter.enqueue(payload)
            flush_summary = reporter.flush()
            if flush_summary.get("sent") or flush_summary.get("invalid"):
                log(
                    f"指标上报成功：{flush_summary['sent']} 条，"
                    f"无效丢弃 {flush_summary['invalid']} 条，待补报 {flush_summary['pending']} 条"
                )
        except Exception as exc:  # noqa: BLE001 - 采集循环不因单次异常退出
            log(f"采集异常：{exc}")
        if args.once:
            replay_results()
            heartbeat.send_once()
            return 0
        elapsed = time.monotonic() - tick_started
        _STOP.wait(max(1.0, telemetry_seconds - elapsed))
    journal.close()
    log("采集进程退出")
    return 0


# --------------------------------------------------------------------------- 执行进程
def _executor_loop(config: dict[str, Any]) -> int:
    client = build_client(config)
    journal = open_journal(config)
    runner = executor_module.Executor()
    job_runner = JobRunner(
        client,
        journal,
        config,
        executor=runner.execute,
        logger=log,
        lease_seconds=int(config.get("job_lease_seconds") or JOB_LEASE_SECONDS),
        renew_seconds=int(config.get("job_lease_renew_seconds") or JOB_LEASE_RENEW_SECONDS),
    )
    recovered = job_runner.recover_on_start()
    if recovered:
        log(f"启动恢复：task_id={recovered.get('task_id')} 已标记 unknown，等待人工核对")
    poll_seconds = int(config.get("job_poll_seconds") or 5)
    log(
        f"Agent {AGENT_VERSION} 执行进程启动：protocol={PROTOCOL_VERSION} "
        f"租约={job_runner.lease_seconds}s 续租={job_runner.renew_seconds}s"
    )
    job_runner.run_loop(_STOP, poll_seconds=poll_seconds)
    journal.close()
    return 0


def cmd_executor(args: argparse.Namespace) -> int:
    config = config_module.load_config(args.config)
    if not config_module.center_url(config):
        print("配置缺少 center，请先执行 enroll", file=sys.stderr)
        return 2
    _install_signal_handlers()
    if args.once:
        client = build_client(config)
        journal = open_journal(config)
        runner = executor_module.Executor()
        job_runner = JobRunner(
            client,
            journal,
            config,
            executor=runner.execute,
            logger=log,
            lease_seconds=int(config.get("job_lease_seconds") or JOB_LEASE_SECONDS),
            renew_seconds=int(config.get("job_lease_renew_seconds") or JOB_LEASE_RENEW_SECONDS),
        )
        job_runner.recover_on_start()
        result = job_runner.run_once()
        print(json.dumps(result, ensure_ascii=False, default=str))
        journal.close()
        return 0 if result.get("status") not in {"blocked_unknown", "error"} else 1
    return _executor_loop(config)


# --------------------------------------------------------------------------- 运维命令
def cmd_heartbeat(args: argparse.Namespace) -> int:
    config = config_module.load_config(args.config)
    client = build_client(config)
    journal = open_journal(config)
    sender = HeartbeatSender(
        client,
        role=str(args.role),
        config=config,
        boot_id=uuid.uuid4().hex,
        state_provider=lambda: {
            "executor": JobRunner(
                client, journal, config, executor=lambda *a, **k: {}, logger=log
            ).status(),
            "journal": journal.status_summary()["counts"],
        },
        logger=log,
    )
    result = sender.send_once()
    print(json.dumps(result, ensure_ascii=False, default=str))
    journal.close()
    return 0 if result.get("ok") else 1


def cmd_status(args: argparse.Namespace) -> int:
    config = config_module.load_config(args.config)
    journal = open_journal(config)
    summary = journal.status_summary()
    journal.close()
    output = {
        "agent_version": AGENT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "config": _config_view(config),
        "journal": summary,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    config = config_module.load_config(args.config)
    journal = open_journal(config)
    client = build_client(config)
    job_runner = JobRunner(client, journal, config, executor=lambda *a, **k: {}, logger=log)
    target: dict[str, Any] | None
    if args.job_id:
        target = journal.get_lease(str(args.job_id))
    else:
        target = journal.active_lease()
    if target is None:
        print("没有需要核对的 active/unknown 任务")
        journal.close()
        return 0
    if str(target.get("phase")) != "unknown" and not args.force:
        print(
            f"任务 {target.get('task_id')} 当前阶段为 {target.get('phase')}，"
            "只有 unknown 任务需要核对（--force 可强制解除占用）"
        )
        journal.close()
        return 2

    # 先与服务端核对：服务端若已确认终态，以服务端为准并直接解除占用
    remote = job_runner.reconcile_remote(
        target, local_status=str(args.status or "unknown"), note=str(args.note or "")
    )
    remote_action = str((remote or {}).get("action") or "")
    remote_status = str((remote or {}).get("server_status") or "")
    if remote_action in {"marked_succeeded", "marked_failed", "already_terminal"} and remote_status in {
        "succeeded",
        "failed",
    }:
        journal.override_result(
            task_id=str(target["task_id"]),
            status=remote_status,
            error=str(args.note or "与服务端核对后确认的终态"),
        )
        journal.update_lease(
            task_id=str(target["task_id"]),
            attempt_id=str(target["attempt_id"]),
            phase=remote_status,
        )
        if args.report:
            job_runner.flush_results(force=True)
        journal.close()
        print(
            json.dumps(
                {
                    "reconciled": True,
                    "source": "server",
                    "action": remote_action,
                    "server_status": remote_status,
                    "remote": remote,
                },
                ensure_ascii=False,
                default=str,
            )
        )
        return 0

    if args.status:
        journal.override_result(
            task_id=str(target["task_id"]),
            status=str(args.status),
            error=str(args.note or "人工核对"),
        )
    lease = journal.reconcile(task_id=str(target["task_id"]), note=str(args.note or ""))
    if args.report:
        job_runner.flush_results(force=True)
    journal.close()
    print(
        json.dumps(
            {"reconciled": True, "source": "local", "lease": lease, "remote": remote},
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


def cmd_self_upgrade(args: argparse.Namespace) -> int:
    config = config_module.load_config(args.config)
    root = config_module.agent_root(config)
    units = [str(item) for item in config_module.units(config)]
    runner = executor_module.SubprocessCommandRunner()
    result = upgrade_module.self_upgrade(
        agent_root=root,
        source=args.source,
        version=args.version,
        commit=args.commit or "",
        config_path=args.config,
        units=units,
        runner=runner,
        restart=not args.no_restart,
        dry_run=args.dry_run,
        logger=log,
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


def cmd_selftest(_args: argparse.Namespace) -> int:
    from agentlib.selftest import run_selftest

    return run_selftest()


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ERP 租户服务器 Agent（Agent 2）")
    sub = parser.add_subparsers(dest="command", required=True)

    enroll = sub.add_parser("enroll", help="使用一次性令牌注册节点")
    enroll.add_argument("--center", required=True, help="管理中心地址，如 https://erp.example.com")
    enroll.add_argument("--token", required=True, help="后台生成的一次性接入令牌")
    enroll.add_argument("--instance", type=int, default=0, help="实例ID（仅记录用）")
    enroll.add_argument("--config", default="/etc/erp-agent/config.json")
    enroll.set_defaults(func=cmd_enroll)

    run = sub.add_parser("run", help="采集进程：指标 + 心跳 + 缓存/结果补报")
    run.add_argument("--config", default="/etc/erp-agent/config.json")
    run.add_argument("--once", action="store_true", help="只采集/补报一次后退出")
    run.add_argument(
        "--role",
        choices=["collector", "executor", "all"],
        default="collector",
        help="collector=仅采集（默认）；executor=仅执行；all=同进程（旧部署兼容）",
    )
    run.set_defaults(func=cmd_run)

    executor_cmd = sub.add_parser("executor", help="执行进程：租约任务执行与结果落库")
    executor_cmd.add_argument("--config", default="/etc/erp-agent/config.json")
    executor_cmd.add_argument("--once", action="store_true", help="只轮询/执行一次后退出")
    executor_cmd.set_defaults(func=cmd_executor)

    heartbeat = sub.add_parser("heartbeat", help="发送一次 Agent 心跳（排障用）")
    heartbeat.add_argument("--config", default="/etc/erp-agent/config.json")
    heartbeat.add_argument("--role", default="collector")
    heartbeat.set_defaults(func=cmd_heartbeat)

    status = sub.add_parser("status", help="查看本地执行日志与占用状态")
    status.add_argument("--config", default="/etc/erp-agent/config.json")
    status.set_defaults(func=cmd_status)

    reconcile = sub.add_parser("reconcile", help="人工核对 unknown 任务，解除实例占用")
    reconcile.add_argument("--config", default="/etc/erp-agent/config.json")
    reconcile.add_argument("--job-id", default="", help="任务ID（默认取当前活动租约）")
    reconcile.add_argument("--status", choices=["succeeded", "failed", "unknown"], default="", help="核对后上报的终态")
    reconcile.add_argument("--note", default="", help="核对说明（写入本地日志与结果）")
    reconcile.add_argument("--force", action="store_true", help="即使不是 unknown 也强制解除占用")
    reconcile.add_argument("--no-report", dest="report", action="store_false", help="不立即上报结果")
    reconcile.set_defaults(func=cmd_reconcile, report=True)

    upgrade = sub.add_parser("self-upgrade", help="Agent 自身升级（独立版本目录 + 重启验证）")
    upgrade.add_argument("--config", default="/etc/erp-agent/config.json")
    upgrade.add_argument("--source", required=True, help="Agent 源码目录或 tar.gz 离线包")
    upgrade.add_argument("--version", required=True, help="目标 Agent 版本（如 2.1.0）")
    upgrade.add_argument("--commit", default="", help="目标完整 commit（可选，40/64 位）")
    upgrade.add_argument("--no-restart", action="store_true", help="只切版本不重启（调试用）")
    upgrade.add_argument("--dry-run", action="store_true", help="只校验与暂存，不切链不重启")
    upgrade.set_defaults(func=cmd_self_upgrade)

    selftest = sub.add_parser("selftest", help="运行离线自测（不依赖真实服务器）")
    selftest.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(verbose=bool(getattr(args, "verbose", False)))
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        log("收到中断信号")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
