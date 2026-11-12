#!/usr/bin/env python3
"""ERP 租户服务器采集 Agent（仅标准库，Linux）。

用法：
  sudo python3 erp_agent.py enroll --center https://erp.example.com \
      --token <一次性令牌> --config /etc/erp-agent/config.json
  sudo python3 erp_agent.py run --config /etc/erp-agent/config.json

设计约束：
* 只采集宿主机与容器状态，不执行任意命令。
* 30 秒上报一次；断网时本地最多缓存 24 小时，恢复后按原采样时间补报。
* 节点身份固定在注册时写入的 node_uuid + node_token，IP 变化不影响身份。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AGENT_VERSION = "0.1.0"
DEFAULT_CONFIG = "/etc/erp-agent/config.json"
CACHE_LIMIT = 2880  # 24h / 30s
REPORT_SECONDS = 30


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        value = rest.strip().split(" ")[0] if rest.strip() else "0"
        if value.isdigit():
            result[key] = int(value) * 1024
    return result


def _cpu_percent(interval: float = 0.4) -> float:
    def snapshot() -> tuple[int, int]:
        line = _read("/proc/stat").splitlines()[0] if _read("/proc/stat") else ""
        parts = [int(item) for item in line.split()[1:] if item.isdigit()]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        return total, idle

    total1, idle1 = snapshot()
    time.sleep(interval)
    total2, idle2 = snapshot()
    total_delta = total2 - total1
    idle_delta = idle2 - idle1
    if total_delta <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 2)


def _load() -> tuple[float, float, float]:
    try:
        one, five, fifteen = os.getloadavg()
        return round(one, 2), round(five, 2), round(fifteen, 2)
    except OSError:
        return 0.0, 0.0, 0.0


def _internal_ips() -> list[str]:
    ips: list[str] = []
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = item[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if ip not in ips and not ip.startswith("127."):
            ips.append(ip)
    except OSError:
        pass
    return ips


def _public_ip() -> str:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=4) as response:
            value = response.read().decode("utf-8").strip()
            return value if len(value) <= 64 else ""
    except Exception:  # noqa: BLE001 - 公网 IP 获取失败不应影响指标上报
        return ""


def _disk() -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    mounts = [("/", "根分区")]
    for line in _read("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, fstype = parts[0], parts[1], parts[2]
        if not device.startswith("/dev/") or fstype in {"tmpfs", "devtmpfs", "overlay", "squashfs"}:
            continue
        if mount in {"/boot", "/boot/efi"} and mount not in {item[0] for item in mounts}:
            mounts.append((mount, "启动分区"))
    for mount, label in mounts:
        try:
            stat = os.statvfs(mount)
            total = stat.f_frsize * stat.f_blocks
            free = stat.f_bavail * stat.f_frsize
            used = total - free
            percent = round(used / total * 100, 2) if total else 0
            disks.append(
                {
                    "mount": mount,
                    "label": label,
                    "total_gb": round(total / 1024**3, 1),
                    "used_gb": round(used / 1024**3, 1),
                    "free_gb": round(free / 1024**3, 1),
                    "percent": percent,
                    "inodes_percent": round(
                        (stat.f_files - stat.f_favail) / stat.f_files * 100, 2
                    )
                    if stat.f_files
                    else 0,
                }
            )
        except OSError:
            continue
    return disks


def _network() -> dict[str, Any]:
    rx = tx = 0
    for line in _read("/proc/net/dev").splitlines()[2:]:
        if ":" not in line:
            continue
        _, data = line.split(":", 1)
        fields = data.split()
        if len(fields) >= 9:
            rx += int(fields[0])
            tx += int(fields[8])
    return {"rx_bytes": rx, "tx_bytes": tx}


def _docker_services() -> dict[str, Any]:
    if not shutil.which("docker"):
        return {"available": False, "containers": []}
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "containers": []}
    containers: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        health = "healthy" if "(healthy)" in parts[2] else (
            "unhealthy" if "(unhealthy)" in parts[2] else ""
        )
        containers.append(
            {"name": parts[0], "state": parts[1], "status": parts[2], "health": health}
        )
    return {"available": True, "containers": containers}


def collect(previous_network: dict[str, Any] | None = None) -> dict[str, Any]:
    mem = _meminfo()
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", mem.get("MemFree", 0))
    used = max(0, total - available)
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    load1, load5, load15 = _load()
    network = _network()
    net_rate: dict[str, Any] = {}
    if previous_network:
        net_rate = {
            "rx_bytes_per_sec": max(
                0, network["rx_bytes"] - int(previous_network.get("rx_bytes", 0))
            )
            // REPORT_SECONDS,
            "tx_bytes_per_sec": max(
                0, network["tx_bytes"] - int(previous_network.get("tx_bytes", 0))
            )
            // REPORT_SECONDS,
        }
    network.update(net_rate)
    return {
        "sampled_at": utc_now(),
        "hostname": socket.gethostname()[:255],
        "os_name": platform.platform()[:100],
        "arch": platform.machine()[:32],
        "cpu_cores": os.cpu_count() or 0,
        "memory_total_mb": total // 1024**2,
        "disk_total_gb": int(sum(item["total_gb"] for item in _disk())),
        "internal_ips": _internal_ips(),
        "public_ip": _public_ip(),
        "agent_version": AGENT_VERSION,
        "app_version": os.environ.get("ERP_APP_VERSION", ""),
        "cpu_percent": _cpu_percent(),
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "memory_used_mb": used // 1024**2,
        "memory_available_mb": available // 1024**2,
        "memory_percent": round(used / total * 100, 2) if total else 0,
        "swap_total_mb": swap_total // 1024**2,
        "swap_used_mb": (swap_total - swap_free) // 1024**2,
        "disks": _disk(),
        "network": network,
        "services": {"docker": _docker_services()},
    }


def http_json(url: str, payload: dict[str, Any] | None, token: str = "", timeout: int = 10) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", f"erp-agent/{AGENT_VERSION}")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def load_config(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_config(path: str, config: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def enroll(args: argparse.Namespace) -> int:
    base = args.center.rstrip("/")
    metrics = collect()
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
        "metadata": {"instance_id": args.instance, "python": platform.python_version()},
    }
    result = http_json(f"{base}/api/v1/agent/enroll", payload)
    data = result.get("data") or {}
    token = data.get("node_token")
    if not token:
        print("注册失败：响应缺少 node_token", result)
        return 1
    save_config(
        args.config,
        {
            "center": base,
            "node_id": data.get("node_id"),
            "node_uuid": data.get("node_uuid"),
            "node_token": token,
            "instance_id": data.get("instance_id"),
        },
    )
    print(f"注册成功：node_id={data.get('node_id')} node_uuid={data.get('node_uuid')}")
    print(f"配置已保存：{args.config}")
    return 0


def _cache_path(config_path: str) -> Path:
    return Path(config_path).with_name("telemetry-cache.json")


def _read_cache(config_path: str) -> list[dict[str, Any]]:
    try:
        return json.loads(_cache_path(config_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _write_cache(config_path: str, items: list[dict[str, Any]]) -> None:
    try:
        _cache_path(config_path).parent.mkdir(parents=True, exist_ok=True)
        _cache_path(config_path).write_text(
            json.dumps(items[-CACHE_LIMIT:], ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def poll_and_execute(config_path: str, base: str, token: str) -> None:
    """领取一个运维任务并在本机受限执行。"""
    from executor import execute  # 与 erp_agent.py 同目录

    config = load_config(config_path)
    result = http_json(f"{base}/api/v1/agent/jobs/poll", {}, token=token)
    job = (result.get("data") or {}).get("job")
    if not job:
        return

    def report(step_no: int, step_name: str, status: str, message: str) -> None:
        http_json(
            f"{base}/api/v1/agent/jobs/{job['id']}/events",
            {
                "status": "running",
                "step_no": step_no,
                "step_name": step_name,
                "step_status": status,
                "message": message,
            },
            token=token,
        )

    outcome = execute(job, config, report)
    final_status = "succeeded" if outcome.get("ok") else "failed"
    http_json(
        f"{base}/api/v1/agent/jobs/{job['id']}/events",
        {
            "status": final_status,
            "result": {k: v for k, v in outcome.items() if k != "output"},
            "error": "" if outcome.get("ok") else str(outcome.get("error") or outcome.get("exit_code") or "执行失败")[:500],
        },
        token=token,
    )


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    base = str(config["center"]).rstrip("/")
    token = str(config["node_token"])
    previous_network: dict[str, Any] | None = None
    print(f"Agent 启动：center={base} node_id={config.get('node_id')}")
    while True:
        try:
            payload = collect(previous_network)
            previous_network = payload["network"]
            cache = _read_cache(args.config)
            cache.append(payload)
            _write_cache(args.config, cache)
            remaining: list[dict[str, Any]] = []
            for item in cache:
                try:
                    http_json(f"{base}/api/v1/agent/telemetry", item, token=token)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                    print(f"上报失败，保留本地缓存：{exc}")
                    remaining.append(item)
            _write_cache(args.config, remaining)
        except Exception as exc:  # noqa: BLE001 - 采集循环不能因单次异常退出
            print(f"采集异常：{exc}")
        try:
            poll_and_execute(args.config, base, token)
        except Exception as exc:  # noqa: BLE001 - 任务失败不影响指标采集
            print(f"任务轮询异常：{exc}")
        if args.once:
            return 0
        time.sleep(REPORT_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description="ERP 租户服务器采集 Agent")
    sub = parser.add_subparsers(dest="command", required=True)
    enroll_parser = sub.add_parser("enroll", help="使用一次性令牌注册")
    enroll_parser.add_argument("--center", required=True, help="管理中心地址，如 https://erp.example.com")
    enroll_parser.add_argument("--token", required=True, help="后台生成的一次性接入令牌")
    enroll_parser.add_argument("--instance", type=int, default=0, help="实例ID（仅记录用）")
    enroll_parser.add_argument("--config", default=DEFAULT_CONFIG)
    enroll_parser.set_defaults(func=enroll)
    run_parser = sub.add_parser("run", help="启动周期上报")
    run_parser.add_argument("--config", default=DEFAULT_CONFIG)
    run_parser.add_argument("--once", action="store_true", help="只采集上报一次后退出")
    run_parser.set_defaults(func=run)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
