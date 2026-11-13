"""SSH 加固（task-25）单元测试：仅 SQLite 内存中心库，不连接真实 SSH / MySQL / Redis。

覆盖：
* SSH 目标只允许管理员显式配置，Agent 上报 IP 不回退为连接目标；
* 主机指纹首次记录 / 已记录匹配 / 不匹配拒绝 / 管理员重置 / 目标变更清空；
* 连接前指纹校验策略（Paramiko 认证之前拒绝）；
* 命令审计脱敏、连接/命令/断开审计、终端常量与 30s 会话复核。
"""

from __future__ import annotations

import io

import paramiko
import pytest
from pydantic import ValidationError
from sqlalchemy import Integer, MetaData, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import AuthenticatedUser
from app.core.errors import AppError, ErrorCode
from app.db.base import CenterBase
from app.modules.ops import service
from app.modules.ops.models_center import ServerNode, TenantInstance
from app.modules.ops.schemas import NodeSshConfigIn, NodeSshFingerprintResetIn
from app.modules.platform.models_center import PlatformAuditLog

CENTER_TABLES = ("server_nodes", "tenant_instances", "platform_audit_logs")


def _sqlite_metadata() -> MetaData:
    clone = MetaData()
    for name in CENTER_TABLES:
        table = CenterBase.metadata.tables[name].to_metadata(clone)
        id_column = table.columns.get("id")
        if id_column is not None:
            id_column.type = Integer()
            id_column.autoincrement = True
    return clone


@pytest.fixture()
def center():
    import app.modules.ops.models_center  # noqa: F401
    import app.modules.platform.models_center  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    _sqlite_metadata().create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    session.add(
        TenantInstance(
            id=1,
            tenant_id=1,
            name="演示实例",
            domain="demo.example.com",
            status="active",
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def seed_node(center, *, ssh_host: str = "10.0.0.8") -> ServerNode:
    from app.core.security import encrypt_secret

    node = ServerNode(
        id=1,
        instance_id=1,
        tenant_id=1,
        node_uuid="11111111-1111-1111-1111-111111111111",
        hostname="node-1",
        token_hash="hash",
        status="online",
        ssh_host=ssh_host,
        ssh_port=22,
        ssh_username_cipher=encrypt_secret("root"),
        ssh_password_cipher=encrypt_secret("super-secret-password"),
    )
    center.add(node)
    center.commit()
    return node


class FakeKey:
    """最小 Paramiko PKey 替身：指纹只依赖 asbytes()/get_name()。"""

    def __init__(self, data: bytes = b"host-key-material", name: str = "ssh-ed25519") -> None:
        self._data = data
        self._name = name

    def asbytes(self) -> bytes:
        return self._data

    def get_name(self) -> str:
        return self._name


def audit_actions(center) -> list[str]:
    return [
        row[0]
        for row in center.execute(
            select(PlatformAuditLog.action).order_by(PlatformAuditLog.id.asc())
        ).all()
    ]


# --------------------------------------------------------------------------- 目标不回退
def test_ssh_target_never_falls_back_to_agent_reported_ip(center):
    node = seed_node(center, ssh_host="")
    node.observed_public_ip = "203.0.113.9"
    node.configured_public_ip = "198.51.100.7"
    node.internal_ips = ["10.1.1.1"]
    center.commit()

    config = service.get_ssh_config(center, node.id)
    assert config["host"] == ""
    assert config["host_configured"] is False
    assert config["detected_host"] == "203.0.113.9"

    with pytest.raises(AppError) as exc_info:
        service.ssh_connect_params(center, node.id)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert "显式" in exc_info.value.message


def test_ssh_host_schema_rejects_url_path_and_account():
    for bad in ("https://1.2.3.4", "host/with/path", "root@1.2.3.4", "host name"):
        with pytest.raises(ValidationError):
            NodeSshConfigIn(host=bad)
    assert NodeSshConfigIn(host="1.2.3.4").host == "1.2.3.4"
    assert NodeSshConfigIn(host="[2001:db8::1]").host == "[2001:db8::1]"


# --------------------------------------------------------------------------- 指纹
def test_ssh_key_fingerprint_and_pre_auth_policy():
    key = FakeKey()
    digest, algorithm = service.ssh_key_fingerprint(key)
    assert digest.startswith("SHA256:")
    assert algorithm == "ssh-ed25519"
    assert len(digest) > len("SHA256:") + 20

    policy = service._new_ssh_host_key_policy("")
    policy.missing_host_key(None, "host", key)  # 首次：允许并记录
    assert policy.fingerprint == digest
    assert policy.mismatch is False

    mismatch_policy = service._new_ssh_host_key_policy("SHA256:WRONG")
    with pytest.raises(paramiko.SSHException):
        mismatch_policy.missing_host_key(None, "host", key)
    assert mismatch_policy.mismatch is True


def test_fingerprint_first_record_match_and_mismatch(center):
    node = seed_node(center)
    fingerprint, algorithm = service.ssh_key_fingerprint(FakeKey())

    first = service.confirm_ssh_host_fingerprint(
        center,
        node,
        fingerprint,
        algorithm,
        actor_user_id=99,
        request_id="req-1",
        ip="203.0.113.5",
        host="10.0.0.8",
    )
    assert first["recorded"] is True
    assert node.ssh_host_fingerprint == fingerprint
    assert node.ssh_fingerprint_algorithm == "ssh-ed25519"
    assert node.ssh_host_fingerprint_verified_at is not None
    assert audit_actions(center) == ["ssh.fingerprint.recorded"]

    config = service.get_ssh_config(center, node.id)
    assert config["ssh_host_fingerprint"] == fingerprint
    assert config["fingerprint_status"] == "verified"
    assert str(config["ssh_host_fingerprint_verified_at"]).endswith("Z")

    # 已记录且一致：不重复写审计/不覆盖
    matched = service.confirm_ssh_host_fingerprint(
        center, node, fingerprint, algorithm, actor_user_id=99
    )
    assert matched["recorded"] is False
    assert audit_actions(center) == ["ssh.fingerprint.recorded"]

    # 不一致：拒绝并审计
    with pytest.raises(AppError) as exc_info:
        service.confirm_ssh_host_fingerprint(
            center,
            node,
            "SHA256:CHANGED",
            "ssh-ed25519",
            actor_user_id=99,
            host="10.0.0.8",
        )
    error = exc_info.value
    assert error.code == ErrorCode.CONFLICT
    assert error.status_code == 409
    assert error.details["reason"] == "fingerprint_mismatch"
    assert error.details["expected"] == fingerprint
    assert error.details["actual"] == "SHA256:CHANGED"
    assert "ssh.fingerprint.mismatch" in audit_actions(center)
    assert node.ssh_host_fingerprint == fingerprint


def test_fingerprint_reset_allows_new_record(center):
    node = seed_node(center)
    fingerprint, algorithm = service.ssh_key_fingerprint(FakeKey())
    service.confirm_ssh_host_fingerprint(
        center, node, fingerprint, algorithm, actor_user_id=99
    )
    reset = service.reset_ssh_host_fingerprint(
        center, node.id, actor_user_id=99, request_id="reset-1", ip="203.0.113.5"
    )
    assert reset["reset"] is True
    assert reset["ssh_host_fingerprint"] == ""
    assert reset["ssh_host_fingerprint_verified_at"] is None
    assert reset["fingerprint_status"] == "unverified"
    assert "ssh.fingerprint.reset" in audit_actions(center)

    new_fingerprint, new_algorithm = service.ssh_key_fingerprint(FakeKey(b"new-key"))
    recorded = service.confirm_ssh_host_fingerprint(
        center, node, new_fingerprint, new_algorithm, actor_user_id=99
    )
    assert recorded["recorded"] is True
    assert node.ssh_host_fingerprint == new_fingerprint


def test_reset_schema_requires_explicit_confirm():
    assert NodeSshFingerprintResetIn(confirm=True).confirm is True
    with pytest.raises(ValidationError):
        NodeSshFingerprintResetIn(confirm=False)


def test_save_ssh_config_target_change_clears_fingerprint(center):
    node = seed_node(center)
    fingerprint, algorithm = service.ssh_key_fingerprint(FakeKey())
    service.confirm_ssh_host_fingerprint(
        center, node, fingerprint, algorithm, actor_user_id=99
    )

    # 仅改账号/密码：保留指纹
    service.save_ssh_config(
        center,
        node.id,
        NodeSshConfigIn(host="10.0.0.8", port=22, username="ops"),
        actor_user_id=99,
        request_id="save-1",
    )
    assert node.ssh_host_fingerprint == fingerprint

    # 目标 host 变更：清空旧指纹并审计
    result = service.save_ssh_config(
        center,
        node.id,
        NodeSshConfigIn(host="10.0.0.9", port=2222),
        actor_user_id=99,
        request_id="save-2",
    )
    assert result["host"] == "10.0.0.9"
    assert result["port"] == 2222
    assert result["ssh_host_fingerprint"] == ""
    assert result["fingerprint_status"] == "unverified"
    assert "ssh.fingerprint.reset" in audit_actions(center)
    assert "ssh.config.update" in audit_actions(center)


# --------------------------------------------------------------------------- 命令审计
def test_sanitize_ssh_command_masks_secrets_and_truncates():
    command = (
        "mysql -pSecr3t --password=Secr3t -e 'select 1'; "
        "export TOKEN=abc123; "
        "curl https://user:pass@example.com/api --header 'Authorization: Bearer xyz.123'; "
        "echo done"
    )
    sanitized = service.sanitize_ssh_command(command)
    assert "Secr3t" not in sanitized
    assert "abc123" not in sanitized
    assert "user:pass@" not in sanitized
    assert "xyz.123" not in sanitized
    assert "***" in sanitized
    auth_sanitized = service.sanitize_ssh_command("Authorization: Basic dXNlcjpwYXNz")
    assert "dXNlcjpwYXNz" not in auth_sanitized
    assert "authorization" in auth_sanitized.lower()
    assert "***" in auth_sanitized
    assert len(service.sanitize_ssh_command("x" * 2000)) == 500
    assert "password" not in service.sanitize_ssh_command("password=topsecret").split("=")[1]


class _FakeStream:
    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)
        self.channel = type("Channel", (), {"recv_exit_status": lambda _self: 0})()

    def read(self) -> bytes:
        return self._buffer.read()


class _FakeSSHClient:
    def __init__(self) -> None:
        self.closed = False

    def exec_command(self, command: str, timeout: int | None = None):
        del command, timeout
        return io.BytesIO(b""), _FakeStream(b"command-ok"), _FakeStream(b"")

    def close(self) -> None:
        self.closed = True


def test_ssh_execute_audits_redacted_command_and_disconnect(center, monkeypatch):
    node = seed_node(center)
    fake_client = _FakeSSHClient()
    params = {
        "host": "10.0.0.8",
        "port": 22,
        "username": "root",
        "password": "super-secret-password",
        "node_id": node.id,
    }
    info = {"fingerprint": "SHA256:ABC", "algorithm": "ssh-ed25519", "recorded": False}

    monkeypatch.setattr(
        service,
        "_open_ssh_client",
        lambda center_arg, node_arg, params_arg, **kwargs: (fake_client, params_arg, info),
    )
    result = service.ssh_execute(
        center,
        node.id,
        "echo --password=topsecret token=xyz",
        actor_user_id=99,
        request_id="exec-1",
        ip="203.0.113.5",
    )
    assert result["output"] == "command-ok"
    assert result["exit_status"] == 0
    assert result["fingerprint"] == "SHA256:ABC"
    assert fake_client.closed is True

    actions = audit_actions(center)
    assert "ssh.command" in actions
    assert "ssh.disconnect" in actions
    command_row = center.execute(
        select(PlatformAuditLog).where(PlatformAuditLog.action == "ssh.command")
    ).scalar_one()
    assert "topsecret" not in command_row.payload["command"]
    assert "xyz" not in command_row.payload["command"]
    assert "***" in command_row.payload["command"]
    assert command_row.actor_user_id == 99
    assert command_row.payload["exit_status"] == 0


def test_node_payload_includes_fingerprint_summary(center):
    node = seed_node(center)
    node.ssh_host_fingerprint = "SHA256:ABC"
    node.ssh_fingerprint_algorithm = "ssh-ed25519"
    node.ssh_host_fingerprint_verified_at = service._now()
    center.commit()
    payload = service.node_payload(node)
    assert payload["ssh_host"] == "10.0.0.8"
    assert payload["ssh_host_fingerprint"] == "SHA256:ABC"
    assert payload["ssh_fingerprint_algorithm"] == "ssh-ed25519"
    assert str(payload["ssh_host_fingerprint_verified_at"]).endswith("Z")


# --------------------------------------------------------------------------- 会话复核 / 空闲
def test_ssh_session_recheck_detects_revocation(monkeypatch):
    admin = AuthenticatedUser(
        user_id=1,
        email="admin@example.com",
        display_name="Admin",
        status="active",
        is_platform_admin=True,
    )
    monkeypatch.setattr(service, "_read_ws_session", lambda session_id: {"user_id": 1, "scope": "platform"})
    monkeypatch.setattr(service, "_load_ws_user", lambda user_id: admin)
    assert service.ssh_session_recheck("sid", 1) == (True, "")

    monkeypatch.setattr(service, "_read_ws_session", lambda session_id: None)
    assert service.ssh_session_recheck("sid", 1) == (False, "session_revoked")

    monkeypatch.setattr(
        service, "_read_ws_session", lambda session_id: {"user_id": 1, "scope": "tenant"}
    )
    assert service.ssh_session_recheck("sid", 1) == (False, "session_revoked")

    monkeypatch.setattr(service, "_read_ws_session", lambda session_id: {"user_id": 1})
    monkeypatch.setattr(service, "_load_ws_user", lambda user_id: None)
    assert service.ssh_session_recheck("sid", 1) == (False, "account_disabled")

    disabled = AuthenticatedUser(
        user_id=1,
        email="admin@example.com",
        display_name="Admin",
        status="disabled",
        is_platform_admin=True,
    )
    monkeypatch.setattr(service, "_load_ws_user", lambda user_id: disabled)
    assert service.ssh_session_recheck("sid", 1) == (False, "account_disabled")

    not_admin = AuthenticatedUser(
        user_id=1,
        email="admin@example.com",
        display_name="Admin",
        status="active",
        is_platform_admin=False,
    )
    monkeypatch.setattr(service, "_load_ws_user", lambda user_id: not_admin)
    assert service.ssh_session_recheck("sid", 1) == (False, "permission_revoked")


def test_ssh_idle_and_recheck_intervals():
    assert service.SSH_CONNECT_TIMEOUT_SECONDS == 10
    assert service.SSH_SESSION_RECHECK_SECONDS == 30
    assert service.SSH_WS_IDLE_SECONDS == 15 * 60


# --------------------------------------------------------------------------- WebSocket
class _FakeChannel:
    def recv(self, size: int):  # noqa: ANN001 - 模拟 PTY 通道
        import time

        time.sleep(0.02)
        raise TimeoutError

    def send(self, data) -> None:  # noqa: ANN001
        return None

    def resize_pty(self, width: int, height: int) -> None:
        return None

    def close(self) -> None:
        return None


def _patch_ws_helpers(monkeypatch, *, open_result=None, open_error=None, recheck=(True, "")):
    import app.core.redis as redis_core
    from app.core.auth import AuthenticatedUser

    admin = AuthenticatedUser(
        user_id=1,
        email="admin@example.com",
        display_name="Admin",
        status="active",
        is_platform_admin=True,
    )
    monkeypatch.setattr(
        redis_core, "read_session", lambda session_id: {"user_id": 1, "scope": "platform"}
    )
    monkeypatch.setattr(service, "load_platform_ssh_user", lambda user_id: admin)
    monkeypatch.setattr(service, "ssh_prepare_terminal", lambda node_id: {"host": "10.0.0.8"})

    def _open(node_id, **kwargs):  # noqa: ANN001, ANN003
        if open_error is not None:
            raise open_error
        return open_result

    monkeypatch.setattr(service, "ssh_open_terminal", _open)
    monkeypatch.setattr(service, "ssh_close_terminal", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "ssh_session_recheck", lambda session_id, user_id: recheck)
    monkeypatch.setattr(service, "SSH_SESSION_RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(service, "SSH_WS_IDLE_SECONDS", 30)


def test_websocket_closes_when_session_permission_revoked(monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import app.main as main_module

    _patch_ws_helpers(
        monkeypatch,
        open_result={"node_id": 1, "client": object(), "channel": _FakeChannel(), "host": "10.0.0.8"},
        recheck=(False, "permission_revoked"),
    )
    client = TestClient(main_module.app)
    client.cookies.set("vs_session", "sid")
    with client.websocket_connect(
        "/api/v1/platform/nodes/1/ssh/ws",
        headers={"origin": "http://127.0.0.1:5173"},
    ) as websocket:
        message = websocket.receive_text()
        assert "失效" in message
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_text()
        assert exc_info.value.code == 4411


def test_websocket_closes_with_4409_on_fingerprint_mismatch(monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import app.main as main_module

    mismatch = AppError(
        ErrorCode.CONFLICT,
        "SSH 主机指纹不匹配，已拒绝连接",
        details={"reason": "fingerprint_mismatch"},
    )
    _patch_ws_helpers(monkeypatch, open_error=mismatch)
    client = TestClient(main_module.app)
    client.cookies.set("vs_session", "sid")
    with client.websocket_connect(
        "/api/v1/platform/nodes/1/ssh/ws",
        headers={"origin": "http://127.0.0.1:5173"},
    ) as websocket:
        message = websocket.receive_text()
        assert "指纹不匹配" in message
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_text()
        assert exc_info.value.code == 4409
