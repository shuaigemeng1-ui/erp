"""task-30：中心外 watchdog 纯逻辑测试（不联网、不发信）。

覆盖：端点解析、探测结果归一化、连续失败/恢复/冷却/延迟告警状态机、
独立 SMTP 配置解析、状态文件原子读写与损坏恢复。
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WATCHDOG_PATH = REPO_ROOT / "deploy" / "watchdog" / "erp_watchdog.py"


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("erp_watchdog_under_test", WATCHDOG_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


watchdog = _load_watchdog()


# --------------------------------------------------------------------------- 配置解析
def test_parse_endpoints_defaults_and_named() -> None:
    endpoints = watchdog.parse_endpoints(None)
    assert [item[1] for item in endpoints] == ["/healthz", "/api/v1/runtime"]
    named = watchdog.parse_endpoints("health=/healthz, runtime=/api/v1/runtime")
    assert named[0][0] == "health"
    assert named[1] == ("runtime", "/api/v1/runtime")
    with pytest.raises(ValueError):
        watchdog.parse_endpoints("healthz")
    with pytest.raises(ValueError):
        watchdog.parse_endpoints(",")


def test_smtp_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ERP_WATCHDOG_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("ERP_WATCHDOG_SMTP_PORT", "587")
    monkeypatch.setenv("ERP_WATCHDOG_SMTP_SECURITY", "starttls")
    monkeypatch.setenv("ERP_WATCHDOG_SMTP_USER", "watchdog@example.com")
    monkeypatch.setenv("ERP_WATCHDOG_SMTP_PASSWORD", "secret")
    monkeypatch.setenv("ERP_WATCHDOG_SMTP_TO", "a@example.com,b@example.com")
    monkeypatch.setenv("ERP_WATCHDOG_DRY_RUN", "1")
    config = watchdog.smtp_config()
    assert config["host"] == "smtp.example.com"
    assert config["port"] == 587
    assert config["security"] == "starttls"
    assert config["sender"] == "watchdog@example.com"
    assert config["recipients"] == ["a@example.com", "b@example.com"]
    assert config["dry_run"] is True


# --------------------------------------------------------------------------- 探测
def test_check_endpoint_success_and_failure() -> None:
    ok = watchdog.check_endpoint(
        "https://erp.example.com",
        "health",
        "/healthz",
        timeout=5,
        request_func=lambda url, timeout: (200, b'{"status":"ok"}'),
    )
    assert ok["ok"] is True and ok["status_code"] == 200 and ok["latency_ms"] >= 0

    failed = watchdog.check_endpoint(
        "https://erp.example.com",
        "runtime",
        "/api/v1/runtime",
        timeout=5,
        request_func=lambda url, timeout: (503, b"down"),
    )
    assert failed["ok"] is False and failed["status_code"] == 503
    assert "HTTP 503" in failed["error"]


def test_check_endpoint_handles_network_error() -> None:
    def boom(url: str, timeout: float):
        raise OSError("connection refused")

    result = watchdog.check_endpoint(
        "https://erp.example.com", "health", "/healthz", timeout=5, request_func=boom
    )
    assert result["ok"] is False
    assert "connection refused" in result["error"]


def test_check_once_requires_all_endpoints() -> None:
    def request(url: str, timeout: float):
        return (200, b"ok") if url.endswith("/healthz") else (500, b"fail")

    result = watchdog.check_once(
        "https://erp.example.com",
        [("health", "/healthz"), ("runtime", "/api/v1/runtime")],
        timeout=5,
        request_func=request,
    )
    assert result["ok"] is False
    assert result["failed_names"] == ["runtime"]
    assert "runtime" in result["detail"]


# --------------------------------------------------------------------------- 状态机
def _failure_check(max_latency: int = 100) -> dict[str, Any]:
    return {
        "ok": False,
        "results": [
            {"name": "health", "ok": False, "status_code": 0, "latency_ms": max_latency, "error": "down"}
        ],
        "failed_names": ["health"],
        "max_latency_ms": max_latency,
        "detail": "health=down",
    }


def _ok_check(max_latency: int = 100) -> dict[str, Any]:
    return {
        "ok": True,
        "results": [
            {"name": "health", "ok": True, "status_code": 200, "latency_ms": max_latency, "error": ""}
        ],
        "failed_names": [],
        "max_latency_ms": max_latency,
        "detail": "health=ok",
    }


def test_state_machine_alert_after_threshold_and_recovery() -> None:
    state = watchdog.empty_state()
    # 前两次失败：degraded，不告警
    state, actions = watchdog.decide_transition(
        state, _failure_check(), now=1000.0, fail_threshold=3, cooldown_seconds=1800
    )
    assert actions == [] and state["status"] == "degraded"
    state, actions = watchdog.decide_transition(
        state, _failure_check(), now=1030.0, fail_threshold=3, cooldown_seconds=1800
    )
    assert actions == [] and state["failures"] == 2
    # 第三次失败：告警
    state, actions = watchdog.decide_transition(
        state, _failure_check(), now=1060.0, fail_threshold=3, cooldown_seconds=1800
    )
    assert actions == ["alert"] and state["status"] == "down"
    assert state["down_since"] is not None and state["last_alert_at"] is not None
    # 冷却期内重复失败：不再提醒
    state, actions = watchdog.decide_transition(
        state, _failure_check(), now=1090.0, fail_threshold=3, cooldown_seconds=1800
    )
    assert actions == []
    # 冷却期后：重复提醒
    state, actions = watchdog.decide_transition(
        state, _failure_check(), now=5000.0, fail_threshold=3, cooldown_seconds=1800
    )
    assert actions == ["alert"]
    # 恢复：发恢复邮件并清零
    state, actions = watchdog.decide_transition(
        state, _ok_check(), now=5100.0, fail_threshold=3, cooldown_seconds=1800
    )
    assert actions == ["recovery"] and state["status"] == "up"
    assert state["failures"] == 0 and state["down_since"] is None


def test_state_machine_latency_warning_and_recovery() -> None:
    state = watchdog.empty_state()
    state, actions = watchdog.decide_transition(
        state,
        _ok_check(max_latency=2500),
        now=1000.0,
        fail_threshold=3,
        cooldown_seconds=1800,
        latency_warn_ms=1500,
    )
    assert actions == ["latency_warn"] and state["latency_warn"] is True
    # 仍慢：不重复告警
    state, actions = watchdog.decide_transition(
        state,
        _ok_check(max_latency=2600),
        now=1030.0,
        fail_threshold=3,
        cooldown_seconds=1800,
        latency_warn_ms=1500,
    )
    assert actions == []
    # 恢复：延迟恢复邮件
    state, actions = watchdog.decide_transition(
        state,
        _ok_check(max_latency=100),
        now=1060.0,
        fail_threshold=3,
        cooldown_seconds=1800,
        latency_warn_ms=1500,
    )
    assert actions == ["latency_ok"] and state["latency_warn"] is False


def test_state_machine_recovery_notify_can_be_disabled() -> None:
    state = watchdog.empty_state()
    state, _ = watchdog.decide_transition(
        state, _failure_check(), now=1000.0, fail_threshold=1, cooldown_seconds=60
    )
    assert state["status"] == "down"
    state, actions = watchdog.decide_transition(
        state,
        _ok_check(),
        now=1100.0,
        fail_threshold=1,
        cooldown_seconds=60,
        recovery_notify=False,
    )
    assert actions == []
    assert state["status"] == "up"


# --------------------------------------------------------------------------- 状态文件
def test_state_file_roundtrip_and_corruption(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = watchdog.empty_state()
    state.update({"status": "down", "failures": 3})
    watchdog.save_state(str(path), state)
    loaded = watchdog.load_state(str(path))
    assert loaded["status"] == "down" and loaded["failures"] == 3
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600
    path.write_text("{not-json", encoding="utf-8")
    recovered = watchdog.load_state(str(path))
    assert recovered["status"] == "unknown" and recovered["failures"] == 0


def test_send_mail_dry_run(capsys) -> None:
    config = {
        "dry_run": True,
        "recipients": ["ops@example.com"],
        "host": "smtp.example.com",
        "sender": "watchdog@example.com",
        "subject_prefix": "[ERP]",
    }
    assert watchdog.send_mail(config, "test", "hello") is True
    captured = capsys.readouterr()
    assert "mail_dry_run" in captured.out
    assert "hello" in captured.out


def test_self_test_reports_missing_target(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ERP_WATCHDOG_TARGET", raising=False)
    monkeypatch.setenv("ERP_WATCHDOG_DRY_RUN", "1")
    code = watchdog.self_test()
    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert any("ERP_WATCHDOG_TARGET" in item for item in output["errors"])
