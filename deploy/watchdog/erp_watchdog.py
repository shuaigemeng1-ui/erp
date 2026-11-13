#!/usr/bin/env python3
"""ERP 管理中心独立 watchdog（部署在管理中心之外）。

用途：管理中心自身故障时，管理中心内的告警邮件链路也不可用；本脚本部署在集成网关
服务器或独立小主机上，用**独立 SMTP 配置**探测管理中心并发送告警 / 恢复邮件。

特性：

* 多端点探测：默认 ``/healthz`` + ``/api/v1/runtime``（任一失败按整体失败计）；
* 连续 N 次失败才告警，恢复后发送恢复邮件；未恢复期间按冷却时间重复提醒；
* 探活成功但延迟超过阈值时发送「延迟告警」（可选，默认关闭）；
* 状态写入本地 JSON（原子写入、权限 600），重启不重复告警；
* 独立 SMTP：支持 SSL(465) / STARTTLS(587) / 明文（内网），不读取管理中心配置；
* 无第三方依赖，只使用 Python 标准库；支持 ``--once``、``--dry-run``、``--self-test``。

所有配置来自环境变量（systemd EnvironmentFile），见 ``erp-watchdog.env.example``。
"""

from __future__ import annotations

import json
import os
import smtplib
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

VERSION = "1.0.0"
DEFAULT_STATE = "/var/lib/erp-watchdog/state.json"
DEFAULT_ENDPOINTS = "/healthz,/api/v1/runtime"
DEFAULT_INTERVAL = 60
DEFAULT_FAIL_THRESHOLD = 3
DEFAULT_TIMEOUT = 10
DEFAULT_COOLDOWN = 1800
STATE_VERSION = 1


# --------------------------------------------------------------------------- 工具
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def epoch_iso(value: float) -> str:
    """把 Unix 时间戳转 ISO（状态机测试可注入假时间，生产与 now_iso 等价）。"""
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def log(event: str, **fields: Any) -> None:
    payload = {"ts": now_iso(), "event": event, **fields}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- 配置
def parse_endpoints(value: Optional[str]) -> List[Tuple[str, str]]:
    """解析端点配置：``/healthz,/api/v1/runtime`` 或 ``health=/healthz,runtime=/api/v1/runtime``。"""
    raw = (value or DEFAULT_ENDPOINTS).strip()
    result: List[Tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name = ""
        path = item
        if "=" in item:
            name, path = item.split("=", 1)
            name = name.strip()
            path = path.strip()
        if not path.startswith("/"):
            raise ValueError(f"端点路径必须以 / 开头：{item}")
        if not name:
            name = path.strip("/").replace("/", "_") or "root"
        if name in seen:
            continue
        seen.add(name)
        result.append((name, path))
    if not result:
        raise ValueError("至少配置一个探测端点")
    return result


def smtp_config() -> Dict[str, Any]:
    host = os.environ.get("ERP_WATCHDOG_SMTP_HOST", "").strip()
    recipients = [
        item.strip()
        for item in os.environ.get("ERP_WATCHDOG_SMTP_TO", "").split(",")
        if item.strip()
    ]
    user = os.environ.get("ERP_WATCHDOG_SMTP_USER", "").strip()
    return {
        "host": host,
        "port": _env_int("ERP_WATCHDOG_SMTP_PORT", 465, minimum=1),
        "user": user,
        "password": os.environ.get("ERP_WATCHDOG_SMTP_PASSWORD", ""),
        "sender": os.environ.get("ERP_WATCHDOG_SMTP_FROM", "").strip() or user,
        "recipients": recipients,
        "security": os.environ.get("ERP_WATCHDOG_SMTP_SECURITY", "ssl").strip().lower(),
        "timeout": _env_int("ERP_WATCHDOG_SMTP_TIMEOUT", 15, minimum=3),
        "dry_run": _env_bool("ERP_WATCHDOG_DRY_RUN", False),
        "subject_prefix": os.environ.get("ERP_WATCHDOG_MAIL_SUBJECT_PREFIX", "[ERP]"),
    }


def state_path() -> str:
    return os.environ.get("ERP_WATCHDOG_STATE", DEFAULT_STATE).strip() or DEFAULT_STATE


# --------------------------------------------------------------------------- 状态文件
def empty_state() -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "status": "unknown",
        "failures": 0,
        "down_since": None,
        "last_ok_at": None,
        "last_check_at": None,
        "last_error": "",
        "last_alert_at": None,
        "last_recovery_at": None,
        "last_latency_warn_at": None,
        "total_checks": 0,
        "endpoints": {},
    }


def load_state(path: str) -> Dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict) or int(data.get("version", 0)) != STATE_VERSION:
            return empty_state()
        state = empty_state()
        state.update(data)
        return state
    except (OSError, ValueError, TypeError):
        return empty_state()


def save_state(path: str, state: Dict[str, Any]) -> None:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, target)
    except OSError as exc:
        log("state_save_failed", error=str(exc)[:200])


# --------------------------------------------------------------------------- 探测
def _default_request(url: str, timeout: float) -> Tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return int(response.status), response.read(256)


def check_endpoint(
    target: str,
    name: str,
    path: str,
    *,
    timeout: float,
    request_func: Optional[Callable[[str, float], Tuple[int, bytes]]] = None,
) -> Dict[str, Any]:
    url = target.rstrip("/") + path
    started = time.monotonic()
    request = request_func or _default_request
    try:
        status, body = request(url, timeout)
        latency = int((time.monotonic() - started) * 1000)
        ok = 200 <= int(status) < 400
        return {
            "name": name,
            "url": url,
            "ok": ok,
            "status_code": int(status),
            "latency_ms": latency,
            "error": "" if ok else f"HTTP {status}",
            "body_hint": body[:80].decode("utf-8", "replace"),
        }
    except urllib.error.HTTPError as exc:
        latency = int((time.monotonic() - started) * 1000)
        return {
            "name": name,
            "url": url,
            "ok": False,
            "status_code": int(getattr(exc, "code", 0) or 0),
            "latency_ms": latency,
            "error": f"HTTP {getattr(exc, 'code', 0)}",
            "body_hint": "",
        }
    except (urllib.error.URLError, TimeoutError, OSError, socket.timeout) as exc:
        latency = int((time.monotonic() - started) * 1000)
        return {
            "name": name,
            "url": url,
            "ok": False,
            "status_code": 0,
            "latency_ms": latency,
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "body_hint": "",
        }


def check_once(
    target: str,
    endpoints: Sequence[Tuple[str, str]],
    *,
    timeout: float,
    request_func: Optional[Callable[[str, float], Tuple[int, bytes]]] = None,
) -> Dict[str, Any]:
    results = [
        check_endpoint(target, name, path, timeout=timeout, request_func=request_func)
        for name, path in endpoints
    ]
    failures = [item for item in results if not item["ok"]]
    return {
        "ok": not failures,
        "results": results,
        "failed_names": [item["name"] for item in failures],
        "max_latency_ms": max((int(item["latency_ms"]) for item in results), default=0),
        "detail": "; ".join(
            f"{item['name']}={'ok' if item['ok'] else item['error']}" for item in results
        )[:300],
    }


# --------------------------------------------------------------------------- 状态机（纯函数）
def decide_transition(
    previous: Dict[str, Any],
    check: Dict[str, Any],
    *,
    now: float,
    fail_threshold: int,
    cooldown_seconds: int,
    latency_warn_ms: int = 0,
    recovery_notify: bool = True,
    latency_warn_recovery: bool = True,
) -> Tuple[Dict[str, Any], List[str]]:
    """根据本轮探测结果计算新状态与需要执行的动作。

    动作：``alert``（告警邮件）、``recovery``（恢复邮件）、``latency_warn``（延迟告警）、
    ``latency_ok``（延迟恢复）。纯函数，便于测试。
    """
    state = dict(previous or empty_state())
    state["version"] = STATE_VERSION
    state["total_checks"] = int(state.get("total_checks") or 0) + 1
    checked_at = epoch_iso(now)
    state["last_check_at"] = checked_at
    state["last_error"] = "" if check.get("ok") else str(check.get("detail") or "")[:300]
    state["endpoints"] = {
        item["name"]: {
            "ok": bool(item["ok"]),
            "status_code": int(item.get("status_code") or 0),
            "latency_ms": int(item.get("latency_ms") or 0),
            "error": str(item.get("error") or "")[:200],
            "checked_at": checked_at,
        }
        for item in check.get("results") or []
    }
    actions: List[str] = []
    was_down = str(state.get("status") or "") == "down"
    if check.get("ok"):
        state["failures"] = 0
        state["status"] = "up"
        state["last_ok_at"] = state["last_check_at"]
        state["down_since"] = None
        if was_down and recovery_notify:
            actions.append("recovery")
            state["last_recovery_at"] = state["last_check_at"]
    else:
        failures = int(state.get("failures") or 0) + 1
        state["failures"] = failures
        if failures >= max(1, int(fail_threshold)):
            if not was_down:
                actions.append("alert")
            else:
                last_alert = state.get("last_alert_at")
                if not last_alert or (now - _parse_ts(last_alert)) >= max(60, cooldown_seconds):
                    actions.append("alert")
            state["status"] = "down"
            if not state.get("down_since"):
                state["down_since"] = state["last_check_at"]
            if "alert" in actions:
                state["last_alert_at"] = state["last_check_at"]
        else:
            state["status"] = "degraded"
    # 延迟告警：探活成功但超过阈值（默认关闭），按冷却抑制重复
    max_latency = int(check.get("max_latency_ms") or 0)
    if latency_warn_ms > 0 and check.get("ok"):
        last_warn = state.get("last_latency_warn_at")
        if max_latency >= int(latency_warn_ms) and state.get("latency_warn") is not True:
            actions.append("latency_warn")
            state["latency_warn"] = True
            state["last_latency_warn_at"] = state["last_check_at"]
        elif max_latency < int(latency_warn_ms) and state.get("latency_warn") is True:
            state["latency_warn"] = False
            if latency_warn_recovery:
                actions.append("latency_ok")
    return state, actions


def _parse_ts(value: Any) -> float:
    text = str(value or "")
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- 邮件
def send_mail(
    config: Dict[str, Any], subject: str, text: str, *, dry_run: bool = False
) -> bool:
    prefix = str(config.get("subject_prefix") or "").strip()
    subject_text = f"{prefix} {subject}".strip() if prefix else subject
    if dry_run or config.get("dry_run"):
        log("mail_dry_run", subject=subject_text, recipients=config.get("recipients") or [])
        print(text, flush=True)
        return True
    host = str(config.get("host") or "").strip()
    recipients = list(config.get("recipients") or [])
    sender = str(config.get("sender") or "").strip()
    if not host or not recipients or not sender:
        log("mail_skipped", reason="smtp_not_configured", subject=subject_text)
        return False
    message = EmailMessage()
    message["Subject"] = subject_text
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(text)
    timeout = int(config.get("timeout") or 15)
    security = str(config.get("security") or "ssl").lower()
    try:
        if security == "starttls":
            with smtplib.SMTP(host, int(config.get("port") or 587), timeout=timeout) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                if config.get("user"):
                    smtp.login(str(config["user"]), str(config.get("password") or ""))
                smtp.send_message(message)
        elif security == "none":
            with smtplib.SMTP(host, int(config.get("port") or 25), timeout=timeout) as smtp:
                if config.get("user"):
                    smtp.login(str(config["user"]), str(config.get("password") or ""))
                smtp.send_message(message)
        else:
            with smtplib.SMTP_SSL(
                host,
                int(config.get("port") or 465),
                timeout=timeout,
                context=ssl.create_default_context(),
            ) as smtp:
                if config.get("user"):
                    smtp.login(str(config["user"]), str(config.get("password") or ""))
                smtp.send_message(message)
        log("mail_sent", subject=subject_text, recipients=recipients)
        return True
    except Exception as exc:  # noqa: BLE001 - watchdog 自身不能因发信失败退出
        log("mail_failed", subject=subject_text, error=f"{type(exc).__name__}: {exc}"[:300])
        return False


def _alert_mail(config: Dict[str, Any], target: str, state: Dict[str, Any], check: Dict[str, Any]) -> None:
    send_mail(
        config,
        subject="[严重] 管理中心不可访问",
        text=(
            f"管理中心 {target} 连续 {state.get('failures')} 次探测失败。\n"
            f"时间：{now_iso()}\n"
            f"失败端点：{', '.join(check.get('failed_names') or []) or '-'}\n"
            f"详情：{check.get('detail') or ''}\n"
            f"首次异常：{state.get('down_since') or '-'}\n\n"
            "本邮件由管理中心之外的独立 watchdog 发送；请检查入口网关、容器与数据库。"
        ),
    )


def _recovery_mail(config: Dict[str, Any], target: str, state: Dict[str, Any], check: Dict[str, Any]) -> None:
    send_mail(
        config,
        subject="管理中心已恢复访问",
        text=(
            f"管理中心 {target} 已恢复访问。\n"
            f"时间：{now_iso()}\n"
            f"本次结果：{check.get('detail') or 'ok'}\n"
            f"最近告警时间：{state.get('last_alert_at') or '-'}\n\n"
            "请确认告警是否已恢复、补偿任务是否正常。"
        ),
    )


def _latency_mail(config: Dict[str, Any], target: str, check: Dict[str, Any]) -> None:
    send_mail(
        config,
        subject="[警告] 管理中心响应变慢",
        text=(
            f"管理中心 {target} 探活成功但延迟超过阈值。\n"
            f"时间：{now_iso()}\n"
            f"最大延迟：{check.get('max_latency_ms')} ms\n"
            f"详情：{check.get('detail') or ''}\n"
        ),
    )


# --------------------------------------------------------------------------- 主循环
def run_once(
    *,
    state_file: Optional[str] = None,
    request_func: Optional[Callable[[str, float], Tuple[int, bytes]]] = None,
) -> Dict[str, Any]:
    target = os.environ.get("ERP_WATCHDOG_TARGET", "").strip()
    if not target:
        raise SystemExit("缺少 ERP_WATCHDOG_TARGET（例如 https://erp.example.com）")
    endpoints = parse_endpoints(os.environ.get("ERP_WATCHDOG_ENDPOINTS"))
    timeout = _env_int("ERP_WATCHDOG_TIMEOUT", DEFAULT_TIMEOUT, minimum=1)
    fail_threshold = _env_int("ERP_WATCHDOG_FAIL_THRESHOLD", DEFAULT_FAIL_THRESHOLD, minimum=1)
    cooldown = _env_int("ERP_WATCHDOG_COOLDOWN_SECONDS", DEFAULT_COOLDOWN, minimum=60)
    latency_warn_ms = _env_int("ERP_WATCHDOG_LATENCY_WARN_MS", 0, minimum=0)
    recovery_notify = _env_bool("ERP_WATCHDOG_RECOVERY_NOTIFY", True)
    dry_run = _env_bool("ERP_WATCHDOG_DRY_RUN", False)
    path = state_file or state_path()
    config = smtp_config()
    if dry_run:
        config["dry_run"] = True
    state = load_state(path)
    check = check_once(target, endpoints, timeout=timeout, request_func=request_func)
    state, actions = decide_transition(
        state,
        check,
        now=time.time(),
        fail_threshold=fail_threshold,
        cooldown_seconds=cooldown,
        latency_warn_ms=latency_warn_ms,
        recovery_notify=recovery_notify,
    )
    for action in actions:
        if action == "alert":
            _alert_mail(config, target, state, check)
        elif action == "recovery":
            _recovery_mail(config, target, state, check)
        elif action == "latency_warn":
            _latency_mail(config, target, check)
        elif action == "latency_ok":
            send_mail(
                config,
                subject="管理中心响应已恢复",
                text=f"管理中心 {target} 响应延迟已恢复正常。\n时间：{now_iso()}\n",
            )
    save_state(path, state)
    summary = {
        "ok": bool(check.get("ok")),
        "status": state.get("status"),
        "failures": state.get("failures"),
        "actions": actions,
        "detail": check.get("detail"),
        "max_latency_ms": check.get("max_latency_ms"),
        "state_file": path,
        "endpoints": state.get("endpoints"),
    }
    log("watchdog_cycle", **summary)
    return summary


def self_test() -> int:
    """配置自检：不访问网络、不发信，只校验环境变量与端点解析。"""
    errors: List[str] = []
    target = os.environ.get("ERP_WATCHDOG_TARGET", "").strip()
    if not target:
        errors.append("缺少 ERP_WATCHDOG_TARGET")
    else:
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append("ERP_WATCHDOG_TARGET 必须是 http(s)://host[:port]")
    try:
        endpoints = parse_endpoints(os.environ.get("ERP_WATCHDOG_ENDPOINTS"))
    except ValueError as exc:
        endpoints = []
        errors.append(str(exc))
    config = smtp_config()
    if not config["dry_run"]:
        if not config["host"]:
            errors.append("未配置 ERP_WATCHDOG_SMTP_HOST（或用 ERP_WATCHDOG_DRY_RUN=1 测试）")
        if not config["recipients"]:
            errors.append("未配置 ERP_WATCHDOG_SMTP_TO")
        if not config["sender"]:
            errors.append("未配置 ERP_WATCHDOG_SMTP_FROM/USER")
        if config["security"] not in {"ssl", "starttls", "none"}:
            errors.append("ERP_WATCHDOG_SMTP_SECURITY 只能是 ssl/starttls/none")
    result = {
        "version": VERSION,
        "target": target,
        "endpoints": [{"name": name, "path": path} for name, path in endpoints],
        "state_file": state_path(),
        "smtp": {
            "host": config["host"],
            "port": config["port"],
            "security": config["security"],
            "recipients": config["recipients"],
            "dry_run": config["dry_run"],
        },
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--version" in args:
        print(VERSION)
        return 0
    if "--self-test" in args:
        return self_test()
    once = "--once" in args
    interval = _env_int("ERP_WATCHDOG_INTERVAL", DEFAULT_INTERVAL, minimum=15)
    log(
        "watchdog_start",
        version=VERSION,
        target=os.environ.get("ERP_WATCHDOG_TARGET", ""),
        interval=interval,
        once=once,
    )
    while True:
        try:
            run_once()
        except SystemExit:
            raise
        except KeyboardInterrupt:
            log("watchdog_stop", reason="signal")
            return 0
        except Exception as exc:  # noqa: BLE001 - watchdog 自身异常不能退出
            log("watchdog_error", error=f"{type(exc).__name__}: {exc}"[:300])
        if once:
            return 0
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log("watchdog_stop", reason="signal")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
