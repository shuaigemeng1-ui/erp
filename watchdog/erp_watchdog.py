#!/usr/bin/env python3
"""ERP 管理中心独立 watchdog。

部署在集成网关服务器或独立小主机上，不依赖中心数据库/中心邮件配置：
* 周期请求中心 /healthz 与 /api/v1/runtime
* 连续失败达到阈值后发送告警邮件，恢复后发送恢复邮件
* 状态写入本地文件，重启后避免重复通知

配置全部来自环境变量：
  ERP_WATCHDOG_TARGET=https://erp.example.com
  ERP_WATCHDOG_INTERVAL=60
  ERP_WATCHDOG_FAIL_THRESHOLD=3
  ERP_WATCHDOG_STATE=/var/lib/erp-watchdog/state.json
  ERP_WATCHDOG_SMTP_HOST / PORT / USER / PASSWORD / FROM / TO
  ERP_WATCHDOG_DRY_RUN=1 只打印不发送
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

VERSION = "0.1.0"
DEFAULT_STATE = "/var/lib/erp-watchdog/state.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_state(path: str) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"failures": 0, "down": False, "last_error": ""}


def save_state(path: str, state: dict[str, Any]) -> None:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def check(target: str, timeout: int = 10) -> tuple[bool, str, int]:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(f"{target.rstrip('/')}/healthz", timeout=timeout) as response:
            status = response.status
            body = response.read(200).decode("utf-8", "replace")
        elapsed = int((time.monotonic() - started) * 1000)
        return 200 <= status < 300, f"HTTP {status} {elapsed}ms {body[:80]}", status
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return False, f"{type(exc).__name__}: {exc} ({elapsed}ms)", 0


def send_mail(subject: str, text: str) -> None:
    if os.environ.get("ERP_WATCHDOG_DRY_RUN") == "1":
        print(f"[dry-run] {subject}\n{text}")
        return
    host = os.environ.get("ERP_WATCHDOG_SMTP_HOST", "")
    if not host:
        print("未配置 ERP_WATCHDOG_SMTP_HOST，跳过发信", file=sys.stderr)
        return
    port = int(os.environ.get("ERP_WATCHDOG_SMTP_PORT", "465"))
    user = os.environ.get("ERP_WATCHDOG_SMTP_USER", "")
    password = os.environ.get("ERP_WATCHDOG_SMTP_PASSWORD", "")
    sender = os.environ.get("ERP_WATCHDOG_SMTP_FROM", user)
    recipients = [item.strip() for item in os.environ.get("ERP_WATCHDOG_SMTP_TO", "").split(",") if item.strip()]
    if not recipients:
        print("未配置 ERP_WATCHDOG_SMTP_TO，跳过发信", file=sys.stderr)
        return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(text)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, timeout=15, context=context) as smtp:
        if user:
            smtp.login(user, password)
        smtp.send_message(message)


def run_once(state_path: str) -> dict[str, Any]:
    target = os.environ.get("ERP_WATCHDOG_TARGET", "").strip()
    if not target:
        raise SystemExit("缺少 ERP_WATCHDOG_TARGET")
    threshold = max(1, int(os.environ.get("ERP_WATCHDOG_FAIL_THRESHOLD", "3")))
    state = load_state(state_path)
    ok, detail, status = check(target)
    if ok:
        if state.get("down"):
            send_mail(
                "[ERP] 管理中心已恢复",
                f"管理中心 {target} 已恢复访问。\n时间：{now_iso()}\n探测结果：{detail}",
            )
        state = {"failures": 0, "down": False, "last_error": "", "last_ok_at": now_iso()}
    else:
        failures = int(state.get("failures", 0)) + 1
        was_down = bool(state.get("down"))
        state.update({"failures": failures, "last_error": detail, "last_error_at": now_iso()})
        if failures >= threshold and not was_down:
            state["down"] = True
            send_mail(
                "[ERP][严重] 管理中心不可访问",
                f"管理中心 {target} 连续 {failures} 次探测失败。\n时间：{now_iso()}\n最后错误：{detail}",
            )
        elif was_down:
            print(f"管理中心仍不可用：{detail}")
    save_state(state_path, state)
    print(json.dumps({"ok": ok, "status": status, "detail": detail, "state": state}, ensure_ascii=False))
    return state


def main() -> int:
    state_path = os.environ.get("ERP_WATCHDOG_STATE", DEFAULT_STATE)
    once = "--once" in sys.argv
    interval = max(15, int(os.environ.get("ERP_WATCHDOG_INTERVAL", "60")))
    print(f"ERP watchdog {VERSION} 启动：target={os.environ.get('ERP_WATCHDOG_TARGET', '')} interval={interval}s")
    while True:
        try:
            run_once(state_path)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - watchdog 自身异常不能退出
            print(f"watchdog 探测异常：{exc}", file=sys.stderr)
        if once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
