"""宿主机 / Docker 指标采集与断网缓存补报（仅标准库，Linux）。

从原 ``erp_agent.py`` 拆分出来，供「采集进程」调用；执行器进程不采集指标，
权限与职责分离。采集结果按原始采样时间缓存，网络恢复后按时间顺序补报，
不伪造实时在线状态（中心按 ``sampled_at`` / ``received_at`` 分开存储）。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .constants import AGENT_VERSION, TELEMETRY_CACHE_LIMIT, TELEMETRY_SECONDS


def utc_now() -> str:
    from datetime import datetime, timezone

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


def _disks() -> list[dict[str, Any]]:
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


def detect_app_version(config: dict[str, Any] | None = None) -> str:
    """优先环境变量，否则读取部署目录 .env 的 APP_IMAGE_TAG。"""
    value = os.environ.get("ERP_APP_VERSION", "").strip()
    if value:
        return value
    deploy_dir = Path(str((config or {}).get("deploy_dir") or "/opt/ventasync/deploy"))
    try:
        for line in (deploy_dir / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("APP_IMAGE_TAG="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def collect(
    previous_network: dict[str, Any] | None = None,
    *,
    config: dict[str, Any] | None = None,
    report_seconds: int = TELEMETRY_SECONDS,
) -> dict[str, Any]:
    """采集一次完整指标；``previous_network`` 用于计算网络速率。"""
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
            // max(1, int(report_seconds)),
            "tx_bytes_per_sec": max(
                0, network["tx_bytes"] - int(previous_network.get("tx_bytes", 0))
            )
            // max(1, int(report_seconds)),
        }
    network.update(net_rate)
    return {
        "sampled_at": utc_now(),
        "hostname": socket.gethostname()[:255],
        "os_name": platform.platform()[:100],
        "arch": platform.machine()[:32],
        "cpu_cores": os.cpu_count() or 0,
        "memory_total_mb": total // 1024**2,
        "disk_total_gb": int(sum(item["total_gb"] for item in _disks())),
        "internal_ips": _internal_ips(),
        "public_ip": _public_ip(),
        "agent_version": AGENT_VERSION,
        "app_version": detect_app_version(config),
        "cpu_percent": _cpu_percent(),
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "memory_used_mb": used // 1024**2,
        "memory_available_mb": available // 1024**2,
        "memory_percent": round(used / total * 100, 2) if total else 0,
        "swap_total_mb": swap_total // 1024**2,
        "swap_used_mb": (swap_total - swap_free) // 1024**2,
        "disks": _disks(),
        "network": network,
        "services": {"docker": _docker_services()},
    }


class TelemetryCache:
    """断网缓存：最多 2880 条（24h），按原始采样时间顺序补报。"""

    def __init__(self, path: str | Path, limit: int = TELEMETRY_CACHE_LIMIT) -> None:
        self.path = Path(path)
        self.limit = int(limit)

    def read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def write(self, items: list[dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(items[-self.limit :], ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def append(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        items = self.read()
        items.append(payload)
        items = items[-self.limit :]
        self.write(items)
        return items


def flush_cache(
    cache: TelemetryCache,
    sender: Callable[[dict[str, Any]], Any],
    *,
    logger: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """按采样时间顺序补报缓存；遇到失败即停止，保留该条及之后的所有样本。

    保持时间顺序，避免「后采样的先上报」造成趋势图错序。
    """
    log = logger or (lambda message: None)
    items = cache.read()
    remaining: list[dict[str, Any]] = []
    sent = 0
    failed = False
    for item in items:
        if failed:
            remaining.append(item)
            continue
        try:
            sender(item)
            sent += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败保留缓存继续尝试
            log(f"指标补报失败，保留本地缓存：{exc}")
            failed = True
            remaining.append(item)
    cache.write(remaining)
    return {"sent": sent, "pending": len(remaining)}


class TelemetryBatchSender:
    """批量指标上报（新协议），批量接口不可用时自动回落单条旧格式。"""

    def __init__(
        self,
        client: Any,
        *,
        boot_id: str,
        logger: Callable[[str], None] | None = None,
        batch_size: int = 20,
    ) -> None:
        self.client = client
        self.boot_id = boot_id
        self.log = logger or (lambda message: None)
        self.batch_size = max(1, min(int(batch_size), 20))
        self.unsupported = False

    def send_batch(self, samples: list[dict[str, Any]]) -> dict[str, int]:
        """发送一批样本：accepted/duplicate 视为成功，invalid 丢弃并记录。

        批量接口未上线（404/405/501）或旧服务端拒绝多余字段（400/422）时，
        自动回落为单条旧格式上报。
        """
        from .constants import CAPABILITIES, PROTOCOL_VERSION
        from .httpc import CenterError, EndpointUnsupported, data_of

        if not samples:
            return {"sent": 0, "invalid": 0, "unsupported": 0}
        payload = {
            "boot_id": self.boot_id,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": list(CAPABILITIES),
            "samples": samples,
        }
        try:
            response = self.client.telemetry_batch(payload)
        except (EndpointUnsupported, CenterError) as exc:
            fallback_status = {400, 404, 405, 422, 501}
            if isinstance(exc, CenterError) and exc.status not in fallback_status:
                raise
            if not self.unsupported:
                self.unsupported = True
                self.log("中心未实现批量指标接口，回落单条旧格式上报")
            for sample in samples:
                self.client.telemetry(sample)
            return {"sent": len(samples), "invalid": 0, "unsupported": len(samples)}
        data = data_of(response)
        results = data.get("results")
        if not isinstance(results, list) or len(results) < len(samples):
            # 旧服务端或响应不完整：按整批成功处理（服务端 200 即已接收）
            return {"sent": len(samples), "invalid": 0, "unsupported": 0}
        invalid = 0
        accepted = 0
        for index, item in enumerate(results[: len(samples)]):
            status = str((item or {}).get("status") or "").lower()
            if status in {"accepted", "duplicate"}:
                accepted += 1
            elif status == "invalid":
                invalid += 1
                self.log(
                    f"指标样本被服务端判为无效并丢弃：sample_seq={samples[index].get('sample_seq')} "
                    f"{str((item or {}).get('error') or '')[:120]}"
                )
            else:
                accepted += 1
        return {"sent": accepted, "invalid": invalid, "unsupported": 0}


def flush_telemetry_cache(
    cache: TelemetryCache,
    sender: TelemetryBatchSender,
    *,
    logger: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """按 boot_id + sample_seq 批量补报；网络失败时保留剩余样本（含失败批次）。"""
    log = logger or (lambda message: None)
    items = cache.read()
    sent = invalid = 0
    pending: list[dict[str, Any]] = []
    while items:
        head_boot = str(items[0].get("boot_id") or "")
        batch: list[dict[str, Any]] = []
        while items and len(batch) < sender.batch_size:
            if str(items[0].get("boot_id") or "") != head_boot:
                break
            batch.append(items.pop(0))
        try:
            result = sender.send_batch(batch)
        except Exception as exc:  # noqa: BLE001 - 网络失败保留缓存下次补发
            log(f"指标批量补报失败，保留本地缓存：{exc}")
            pending.extend(batch)
            pending.extend(items)
            break
        sent += int(result.get("sent") or 0)
        invalid += int(result.get("invalid") or 0)
    cache.write(pending)
    return {"sent": sent, "invalid": invalid, "pending": len(pending)}


class TelemetryReporter:
    """指标采样序号与缓存补报（boot_id + sample_seq 供服务端幂等去重）。"""

    def __init__(
        self,
        client: Any,
        cache: TelemetryCache,
        *,
        boot_id: str = "",
        logger: Callable[[str], None] | None = None,
    ) -> None:
        import uuid

        self.client = client
        self.cache = cache
        self.boot_id = boot_id or uuid.uuid4().hex
        self.sample_seq = 0
        self.log = logger or (lambda message: None)
        self.sender = TelemetryBatchSender(client, boot_id=self.boot_id, logger=logger)

    def enqueue(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sample_seq += 1
        enriched = dict(payload)
        enriched["boot_id"] = self.boot_id
        enriched["sample_seq"] = self.sample_seq
        self.cache.append(enriched)
        return enriched

    def flush(self) -> dict[str, int]:
        return flush_telemetry_cache(self.cache, self.sender, logger=self.log)

    @property
    def queued(self) -> int:
        return len(self.cache.read())
