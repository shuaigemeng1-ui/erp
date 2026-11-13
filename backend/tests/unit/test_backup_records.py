"""备份记录关联与恢复回写测试（批次3 bug 修复，不连 MySQL/Redis）。

覆盖 2026-11-13 生产 bug：旧版 Agent 备份任务只登记 ``storage_key``（``db:<stamp>`` 或备份目录），
``local_path``/``checksum`` 为空；恢复演练按本地 run_id/校验值找不到唯一记录，导致
``restore_status`` 无法回写。

本测试验证：

* ``apply_backup_manifest`` 在缺少 ``instance_id`` 时按 local_path/storage_key/run_id 关联旧记录并补齐字段；
* ``find_backup_records`` / ``resolve_backup_record`` 按 run_id + local_path + checksum 精确定位；
* 找不到/多条匹配时给出明确错误，不误写；
* ``record_restore_check`` 通过匹配到的记录写入 verified；
* 校验值格式统一（``sha256:`` 前缀、大小写）。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import Integer, MetaData, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import AppError, ConflictError
from app.db.base import CenterBase
from app.modules.ops import service
from app.modules.ops.models_center import BackupRecord


def _session() -> Session:
    copy = MetaData()
    CenterBase.metadata.tables["backup_records"].to_metadata(copy)
    for table in copy.tables.values():
        table.c.id.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    copy.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _manifest(*, run_id: str = "20261113T075322Z", checksum: str | None = None, instance_id: int | None = None) -> dict[str, Any]:
    run_dir = f"/var/lib/ventasync-backups/releases/{run_id}"
    payload: dict[str, Any] = {
        "run_id": run_id,
        "created_at": _now().isoformat() + "Z",
        "trigger": "scheduled",
        "databases": ["ventasync_center"],
        "config": ["config/.env"],
        "files": [],
        "local": {"status": "verified", "dir": run_dir, "size_bytes": 1024},
        "offsite": {"status": "skipped", "checksum": f"sha256:{_HEX}"},
        "checks": {"sha256_ok": True},
        "retention_days": 30,
    }
    if instance_id is not None:
        payload["instance_id"] = instance_id
    if checksum is not None:
        payload["offsite"]["checksum"] = checksum
    return payload


_HEX = "a" * 64


def _legacy_record(session: Session, *, storage_key: str, instance_id: int = 1) -> BackupRecord:
    record = BackupRecord(
        instance_id=instance_id,
        storage_key=storage_key,
        status="succeeded",
        local_status="generated",
        checksum_status="pending",
        offsite_status="pending",
        started_at=_now(),
        finished_at=_now(),
    )
    session.add(record)
    session.flush()
    return record


def test_apply_manifest_without_instance_associates_legacy_record() -> None:
    """旧 Agent 记录 storage_key='db:<run_id>'、无 local_path/checksum → 补录关联并补齐字段。"""
    session = _session()
    run_id = "20261113T075322Z"
    legacy = _legacy_record(session, storage_key=f"db:{run_id}")
    payload = _manifest(run_id=run_id)

    result = service.apply_backup_manifest(session, payload)
    session.flush()

    assert result["id"] == str(legacy.id)
    assert result["local_path"] == payload["local"]["dir"]
    assert result["checksum"] == f"sha256:{_HEX}"
    assert result["checksum_status"] == "verified"
    assert result["manifest"]["run_id"] == run_id
    assert session.execute(select(func.count()).select_from(BackupRecord)).scalar_one() == 1


def test_apply_manifest_without_instance_associates_record_by_local_path() -> None:
    session = _session()
    run_id = "20261113T080000Z"
    run_dir = f"/var/lib/ventasync-backups/releases/{run_id}"
    legacy = _legacy_record(session, storage_key=run_dir)
    result = service.apply_backup_manifest(session, _manifest(run_id=run_id))
    session.flush()
    assert result["id"] == str(legacy.id)
    assert result["local_path"] == run_dir


def test_apply_manifest_requires_instance_when_no_record_matches() -> None:
    session = _session()
    with pytest.raises(AppError) as excinfo:
        service.apply_backup_manifest(session, _manifest(run_id="20261113T090000Z"))
    message = str(excinfo.value)
    assert "instance_id" in message or "缺少 instance_id" in message


def test_find_and_resolve_by_run_id_local_path_checksum() -> None:
    session = _session()
    payload = service.apply_backup_manifest(session, _manifest(instance_id=1))
    session.flush()
    matches = service.find_backup_records(
        session,
        run_id="20261113T075322Z",
        local_path="/var/lib/ventasync-backups/releases/20261113T075322Z",
        checksum=f"sha256:{_HEX}",
    )
    assert [item["id"] for item in matches] == [payload["id"]]
    resolved = service.resolve_backup_record(
        session,
        run_id="20261113T075322Z",
        local_path="/var/lib/ventasync-backups/releases/20261113T075322Z",
        checksum=_HEX.upper(),  # 大小写/前缀归一化
    )
    assert resolved["id"] == payload["id"]


def test_find_returns_empty_on_checksum_mismatch_and_resolve_raises() -> None:
    session = _session()
    service.apply_backup_manifest(session, _manifest(instance_id=1))
    session.flush()
    assert service.find_backup_records(session, run_id="20261113T075322Z", checksum="sha256:" + "b" * 64) == []
    with pytest.raises(AppError) as excinfo:
        service.resolve_backup_record(session, run_id="20261113T075322Z", checksum="sha256:" + "b" * 64)
    assert "未找到唯一匹配" in str(excinfo.value)


def test_resolve_rejects_ambiguous_records() -> None:
    session = _session()
    for storage_key in ("/var/lib/ventasync-backups/releases/20261113T075322Z", "/backup/copy/20261113T075322Z"):
        _legacy_record(session, storage_key=storage_key)
    session.flush()
    with pytest.raises(ConflictError) as excinfo:
        service.resolve_backup_record(session, run_id="20261113T075322Z")
    assert "多条" in str(excinfo.value)


def test_restore_check_records_verified_via_matched_record() -> None:
    session = _session()
    applied = service.apply_backup_manifest(session, _manifest(instance_id=1))
    session.flush()
    target = service.resolve_backup_record(
        session,
        run_id="20261113T075322Z",
        local_path="/var/lib/ventasync-backups/releases/20261113T075322Z",
        checksum=f"sha256:{_HEX}",
    )
    result = service.record_restore_check(
        session,
        int(target["id"]),
        status="verified",
        notes="隔离恢复硬校验通过",
        actor_user_id=1,
    )
    assert result["id"] == applied["id"]
    assert result["restore_status"] == "verified"
    assert result["restore_verified_at"]
    assert any(stage["name"] == "restore" for stage in result["stages"])


def test_apply_manifest_is_idempotent_and_normalizes_checksum() -> None:
    session = _session()
    first = service.apply_backup_manifest(session, _manifest(instance_id=1, checksum=_HEX.upper()))
    session.flush()
    second = service.apply_backup_manifest(session, _manifest(instance_id=1, checksum=f"sha256:{_HEX}"))
    session.flush()
    assert second["id"] == first["id"]
    assert second["checksum"] == f"sha256:{_HEX}"
    assert session.execute(select(func.count()).select_from(BackupRecord)).scalar_one() == 1


def test_cli_match_kwargs_extracts_criteria() -> None:
    from app.modules.ops.backup_cli import _match_kwargs

    criteria = _match_kwargs(
        {
            "instance_id": "3",
            "run_id": "20261113T075322Z",
            "local_path": "/var/lib/ventasync-backups/releases/20261113T075322Z",
            "checksum": "sha256:" + _HEX,
        }
    )
    assert criteria == {
        "instance_id": 3,
        "run_id": "20261113T075322Z",
        "local_path": "/var/lib/ventasync-backups/releases/20261113T075322Z",
        "checksum": "sha256:" + _HEX,
    }

def _patch_center_session(monkeypatch, session: Session) -> None:
    import app.db.session as db_session

    @contextmanager
    def fake_center_session():  # noqa: ANN202 - 测试替身
        yield session

    monkeypatch.setattr(db_session, "center_session", fake_center_session)


def test_cli_apply_associates_legacy_record_without_instance(monkeypatch, capsys) -> None:
    """CLI apply 复现生产 bug 场景：无 instance_id，按 storage_key/db:<stamp> 关联旧记录。"""
    from app.modules.ops import backup_cli

    session = _session()
    run_id = "20261113T075322Z"
    legacy = _legacy_record(session, storage_key=f"db:{run_id}")
    monkeypatch.delenv("BACKUP_INSTANCE_ID", raising=False)
    monkeypatch.setenv("BACKUP_MANIFEST_JSON", json.dumps(_manifest(run_id=run_id)))
    _patch_center_session(monkeypatch, session)

    assert backup_cli.main(["apply"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["backup"]["id"] == str(legacy.id)
    assert output["backup"]["local_path"].endswith(run_id)
    assert output["backup"]["checksum"] == f"sha256:{_HEX}"


def test_cli_restore_check_resolves_by_run_id_and_checksum(monkeypatch, capsys) -> None:
    from app.modules.ops import backup_cli

    session = _session()
    applied = service.apply_backup_manifest(session, _manifest(instance_id=1))
    session.flush()
    _patch_center_session(monkeypatch, session)
    payload = {
        "run_id": "20261113T075322Z",
        "local_path": "/var/lib/ventasync-backups/releases/20261113T075322Z",
        "checksum": f"sha256:{_HEX}",
        "status": "verified",
        "notes": "CLI 精确匹配回写",
    }
    monkeypatch.setenv("BACKUP_RESTORE_JSON", json.dumps(payload))

    assert backup_cli.main(["restore-check"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["backup"]["id"] == applied["id"]
    assert output["backup"]["restore_status"] == "verified"


def test_cli_restore_check_rejects_ambiguous_match(monkeypatch, capsys) -> None:
    from app.modules.ops import backup_cli

    session = _session()
    _legacy_record(session, storage_key="db:20261113T075322Z")
    _legacy_record(session, storage_key="db:20261113T075322Z")
    session.flush()
    _patch_center_session(monkeypatch, session)
    monkeypatch.setenv(
        "BACKUP_RESTORE_JSON",
        json.dumps({"run_id": "20261113T075322Z", "status": "verified"}),
    )
    assert backup_cli.main(["restore-check"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert "CONFLICT" in output["error"] or "多条" in output["error"]

