#!/usr/bin/env python3
"""ERP 独立网关 SQLite 备份 / 恢复演练 / 保留策略工具（task-45）。

设计约束：
* 只用 SQLite 在线备份 API（``sqlite3.Connection.backup``，等价 ``.backup``），
  **绝不直接 cp WAL/数据库文件**；
* 快照先写入临时目录，``fsync`` 后原子 rename；校验 SHA-256、schema 指纹、
  ``PRAGMA integrity_check``、关键表行数与 webhook 事件计数；
* 加密后保存（AES-256-CTR + HMAC-SHA256，PBKDF2 派生），可复制到异机/独立私有目录；
* 保留策略：已完成事件（delivered/dead/unroutable）原文 7 天、去重记录 30 天；
  pending/retry_wait 等未完成事件永久保留，直到处理结束；
* 恢复演练只在临时目录解密/打开/校验，不接触生产数据卷；演练会模拟 dispatcher
  对 pending 事件做一次租约领取（事务内回滚），证明恢复后事件可被领取；
* 本脚本不 import backend Gateway 代码，schema 通过 PRAGMA/sqlite_master 动态读取。

子命令：
  snapshot       一致性快照 + 校验 + 加密 + 可选异机/私有目录 + 保留清理
  restore-drill  解密到临时目录、校验完整性/schema/行数、模拟 dispatcher 领取
  retention      清理已完成后过期事件（默认 dry-run，--apply 才执行）
  status         只读展示 pending、磁盘已用、按增长率可接收小时数、最近快照
  disk-budget    输出 15 分钟快照 + 分层保留的磁盘预算
  self-test      临时 SQLite 端到端自测（一致性/篡改检测/清理/恢复领取）

依赖：Python 3.10+；加密需要 ``cryptography``（网关镜像已含）。不依赖 backend 包。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import gzip
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

try:  # 延迟到加密/解密时才强制要求；status/retention 可无 cryptography 运行
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
except Exception:  # pragma: no cover
    hashes = None  # type: ignore[assignment]
    Cipher = None  # type: ignore[assignment]
    algorithms = None  # type: ignore[assignment]
    modes = None  # type: ignore[assignment]
    PBKDF2HMAC = None  # type: ignore[assignment]

TOOL_VERSION = "1.0.0"
MAGIC = b"ERPGW1\n"
PBKDF2_ITERATIONS = 200_000
SNAPSHOT_RE = re.compile(r"^gateway-(\d{8}T\d{6}Z)\.db\.enc$")
MANIFEST_RE = re.compile(r"^gateway-(\d{8}T\d{6}Z)\.json$")
FIFTEEN_MIN_SECONDS = 15 * 60
TERMINAL_STATUSES = ("delivered", "dead", "unroutable")
DEFAULT_WEBHOOK_TABLE = "gateway_webhook_events"
CORE_TABLES = (
    "gateway_instances",
    "gateway_channel_apps",
    "gateway_oauth_states",
    "gateway_refresh_locks",
    "gateway_deliveries",
    "gateway_rate_limits",
    "gateway_settings",
    "gateway_audit_logs",
    "gateway_webhook_shops",
    "gateway_webhook_events",
)


class BackupError(RuntimeError):
    """备份/校验/恢复失败（非零退出）。"""


class AlreadyRunning(BackupError):
    """已有备份进程持有锁。"""


# --------------------------------------------------------------------------- 时间/文件
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("空时间")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_snapshot_stamp(name: str) -> datetime | None:
    match = SNAPSHOT_RE.match(name)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fsync_path(path: str | Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            # Windows 对只读句柄 fsync 可能返回 EBADF；Linux 生产环境必须成功。
            if os.name != "nt":
                raise
    finally:
        os.close(descriptor)


def fsync_dir(path: str | Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_replace(src: str | Path, dst: str | Path) -> None:
    fsync_path(src)
    os.replace(src, dst)
    fsync_dir(Path(dst).parent)


def ensure_private_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            target.chmod(0o700)
        except OSError:
            pass
    return target


def write_json_atomic(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    temp = target.with_name(target.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != "nt":
        temp.chmod(0o600)
    atomic_replace(temp, target)


def read_json(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------- 互斥锁
class SnapshotLock:
    """基于 O_EXCL 的跨平台单实例锁；超过 stale_after 的锁视为崩溃残留。"""

    def __init__(self, path: str | Path, *, stale_after_seconds: int = 3 * 3600) -> None:
        self.path = Path(path)
        self.stale_after_seconds = int(stale_after_seconds)
        self._fd: int | None = None
        self._acquired = False

    def __enter__(self) -> "SnapshotLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                self._fd = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                os.write(self._fd, f"{os.getpid()} {iso_z()}\n".encode("utf-8"))
                os.fsync(self._fd)
                self._acquired = True
                return self
            except FileExistsError:
                age = time.time() - self.path.stat().st_mtime
                if attempt == 1 and age > self.stale_after_seconds:
                    with contextlib.suppress(OSError):
                        self.path.unlink()
                    continue
                raise AlreadyRunning(f"已有备份/演练进程持有锁：{self.path}") from None
        raise AlreadyRunning(f"无法获取锁：{self.path}")

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
        if self._acquired:
            with contextlib.suppress(OSError):
                self.path.unlink()


# --------------------------------------------------------------------------- SQLite
def quote_db_path(path: str | Path) -> str:
    return quote(Path(path).resolve().as_posix(), safe="/:")


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    target = Path(path)
    if not target.is_file():
        raise BackupError(f"SQLite 文件不存在：{target}")
    uri = f"file:{quote_db_path(target)}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise BackupError(f"无法只读打开 SQLite：{exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def connect_live(path: str | Path) -> sqlite3.Connection:
    target = Path(path)
    if not target.is_file():
        raise BackupError(f"SQLite 文件不存在：{target}")
    connection = sqlite3.connect(str(target), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def table_columns(connection: sqlite3.Connection, name: str) -> set[str]:
    if not table_exists(connection, name):
        return set()
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({name})")}


def schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        """SELECT type, name, tbl_name, sql FROM sqlite_master
           WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"""
    ).fetchall()
    canonical = json.dumps(
        [[row[0], row[1], row[2], row[3]] for row in rows],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256_text(canonical)


def database_stats(connection: sqlite3.Connection) -> dict[str, Any]:
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0] or 0)
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0] or 0)
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0] or 0)
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0] or "")
    tables: dict[str, int] = {}
    for name in CORE_TABLES:
        if table_exists(connection, name):
            tables[name] = int(
                connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] or 0
            )
    webhook = webhook_stats(connection)
    return {
        "database_bytes": page_count * page_size,
        "page_count": page_count,
        "page_size": page_size,
        "user_version": user_version,
        "journal_mode": journal_mode,
        "schema_sha256": schema_fingerprint(connection),
        "tables": tables,
        "webhook": webhook,
    }


def webhook_stats(connection: sqlite3.Connection) -> dict[str, Any]:
    table = DEFAULT_WEBHOOK_TABLE
    if not table_exists(connection, table):
        return {"available": False}
    columns = table_columns(connection, table)
    counts: dict[str, int] = {}
    for row in connection.execute(
        f"SELECT status, COUNT(*) AS count FROM {table} GROUP BY status"
    ).fetchall():
        counts[str(row["status"])] = int(row["count"] or 0)
    total = sum(counts.values())
    oldest_pending = connection.execute(
        f"SELECT MIN(created_at) FROM {table} WHERE status IN ('pending','retry_wait','processing')"
    ).fetchone()[0]
    terminal = sum(counts.get(status, 0) for status in TERMINAL_STATUSES)
    payload_bytes = 0
    if "payload_cipher" in columns:
        payload_bytes = int(
            connection.execute(
                f"SELECT COALESCE(SUM(LENGTH(payload_cipher)), 0) FROM {table}"
            ).fetchone()[0]
            or 0
        )
    oldest_age = None
    if oldest_pending:
        try:
            oldest_age = max(
                0, int((utc_now() - parse_iso(str(oldest_pending))).total_seconds())
            )
        except ValueError:
            oldest_age = None
    return {
        "available": True,
        "total": total,
        "statuses": counts,
        "pending": int(
            counts.get("pending", 0)
            + counts.get("retry_wait", 0)
            + counts.get("processing", 0)
        ),
        "terminal": terminal,
        "oldest_pending_at": str(oldest_pending) if oldest_pending else None,
        "oldest_pending_age_seconds": oldest_age,
        "payload_bytes": payload_bytes,
    }


def validate_snapshot(path: str | Path) -> tuple[sqlite3.Connection, dict[str, Any]]:
    connection = connect_readonly(path)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise BackupError(f"快照 integrity_check 失败：{integrity}")
        stats = database_stats(connection)
        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_rows:
            raise BackupError("快照存在外键校验失败（本系统应无外键）")
        return connection, stats
    except Exception:
        connection.close()
        raise


# --------------------------------------------------------------------------- 快照
def create_snapshot_db(source: str | Path, destination: str | Path) -> dict[str, Any]:
    """使用 SQLite 在线备份 API 生成一致性快照（自动包含 WAL 已提交内容）。"""
    source_path = Path(source)
    destination_path = Path(destination)
    if destination_path.exists():
        destination_path.unlink()
    source_uri = f"file:{quote_db_path(source_path)}?mode=ro"
    try:
        source_conn = sqlite3.connect(source_uri, uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise BackupError(f"无法打开源数据库：{exc}") from exc
    source_conn.execute("PRAGMA busy_timeout = 30000")
    source_conn.execute("PRAGMA query_only = ON")
    try:
        dest_conn = sqlite3.connect(str(destination_path))
        try:
            # pages=256 + sleep 让在线备份分步进行，避免长时间阻塞网关写入。
            source_conn.backup(dest_conn, pages=256, sleep=0.01)
            dest_conn.commit()
        finally:
            dest_conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite 在线备份失败：{exc}") from exc
    finally:
        source_conn.close()
    fsync_path(destination_path)
    connection, stats = validate_snapshot(destination_path)
    connection.close()
    return stats


# --------------------------------------------------------------------------- 加密
def _crypto_required() -> None:
    if Cipher is None or hashes is None or PBKDF2HMAC is None:
        raise BackupError("缺少 cryptography 依赖，无法加密/解密备份（网关镜像已内置）")


def _derive_keys(passphrase: bytes, salt: bytes) -> tuple[bytes, bytes]:
    _crypto_required()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=64,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    material = kdf.derive(passphrase)
    return material[:32], material[32:]


def read_key_file(key_file: str | Path) -> bytes:
    path = Path(key_file)
    if not path.is_file():
        raise BackupError(f"备份密钥文件不存在：{path}")
    data = path.read_bytes().strip()
    if len(data) < 16:
        raise BackupError("备份密钥文件内容过短（至少 16 字节）")
    return data


def init_key_file(key_file: str | Path, *, force: bool = False) -> str:
    path = Path(key_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise BackupError(f"密钥文件已存在，拒绝覆盖：{path}")
    data = base64.urlsafe_b64encode(os.urandom(48)).strip()
    descriptor = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, data + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name != "nt":
        path.chmod(0o600)
    return str(path)


def compress_file(source: str | Path, destination: str | Path) -> None:
    temp = Path(str(destination) + ".tmp")
    with open(source, "rb") as src, gzip.open(temp, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    fsync_path(temp)
    atomic_replace(temp, destination)


def encrypt_file(source: str | Path, destination: str | Path, key_file: str | Path) -> None:
    """AES-256-CTR + HMAC-SHA256 加密（压缩后的数据），格式见 docs。"""
    _crypto_required()
    passphrase = read_key_file(key_file)
    salt = os.urandom(16)
    iv = os.urandom(16)
    enc_key, mac_key = _derive_keys(passphrase, salt)
    temp = Path(str(destination) + ".tmp")
    digest = hmac.new(mac_key, digestmod=hashlib.sha256)
    header = MAGIC + salt + iv
    digest.update(header)
    with open(source, "rb") as src, open(temp, "wb") as dst:
        dst.write(header)
        encryptor = Cipher(algorithms.AES(enc_key), modes.CTR(iv)).encryptor()
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            ciphertext = encryptor.update(chunk)
            dst.write(ciphertext)
            digest.update(ciphertext)
        final = encryptor.finalize()
        if final:
            dst.write(final)
            digest.update(final)
        dst.write(digest.digest())
        dst.flush()
        os.fsync(dst.fileno())
    if os.name != "nt":
        temp.chmod(0o600)
    atomic_replace(temp, destination)


def _verify_encrypted_hmac(path: str | Path, mac_key: bytes, header: bytes) -> int:
    size = Path(path).stat().st_size
    if size < len(header) + 32:
        raise BackupError("加密备份文件过短")
    digest = hmac.new(mac_key, digestmod=hashlib.sha256)
    digest.update(header)
    with open(path, "rb") as handle:
        handle.seek(len(header))
        remaining = size - len(header) - 32
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise BackupError("加密备份文件被截断")
            digest.update(chunk)
            remaining -= len(chunk)
        stored = handle.read(32)
    if not hmac.compare_digest(digest.digest(), stored):
        raise BackupError("加密备份 HMAC 校验失败（文件被篡改或密钥不正确）")
    return size - len(header) - 32


def decrypt_file(
    source: str | Path, destination: str | Path, key_file: str | Path
) -> None:
    _crypto_required()
    passphrase = read_key_file(key_file)
    with open(source, "rb") as handle:
        header = handle.read(len(MAGIC) + 32)
    if not header.startswith(MAGIC):
        raise BackupError("加密备份格式不正确（magic 不匹配）")
    salt = header[len(MAGIC) : len(MAGIC) + 16]
    iv = header[len(MAGIC) + 16 : len(MAGIC) + 32]
    enc_key, mac_key = _derive_keys(passphrase, salt)
    ciphertext_size = _verify_encrypted_hmac(source, mac_key, header)
    temp = Path(str(destination) + ".tmp")
    with open(source, "rb") as src, open(temp, "wb") as dst:
        src.seek(len(header))
        remaining = ciphertext_size
        decryptor = Cipher(algorithms.AES(enc_key), modes.CTR(iv)).decryptor()
        while remaining > 0:
            chunk = src.read(min(1024 * 1024, remaining))
            if not chunk:
                raise BackupError("加密备份文件被截断")
            plain = decryptor.update(chunk)
            dst.write(plain)
            remaining -= len(chunk)
        final = decryptor.finalize()
        if final:
            dst.write(final)
        dst.flush()
        os.fsync(dst.fileno())
    atomic_replace(temp, destination)


# --------------------------------------------------------------------------- manifest / 保留
def list_snapshots(root: str | Path) -> list[Path]:
    directory = Path(root)
    if not directory.is_dir():
        return []
    return sorted(
        [path for path in directory.iterdir() if SNAPSHOT_RE.match(path.name)],
        key=lambda item: item.name,
        reverse=True,
    )


def manifest_path_for(snapshot: str | Path) -> Path:
    return Path(str(snapshot).replace(".db.enc", ".json"))


def prune_snapshots(
    root: str | Path,
    *,
    recent_hours: int,
    daily_days: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """保留最近 N 小时全部 15m 快照 + 更早 N 天每日一份；其余删除。"""
    root_path = Path(root)
    snapshots = list_snapshots(root_path)
    if not snapshots:
        return {"kept": 0, "deleted": [], "freed_bytes": 0}
    now = utc_now()
    keep: set[Path] = set()
    daily_seen: set[str] = set()
    deleted: list[dict[str, Any]] = []
    for snapshot in snapshots:
        stamp = parse_snapshot_stamp(snapshot.name)
        if stamp is None:
            continue
        age_hours = (now - stamp).total_seconds() / 3600
        if age_hours <= max(0, recent_hours):
            keep.add(snapshot)
            continue
        if age_hours <= max(0, daily_days) * 24:
            day = stamp.strftime("%Y-%m-%d")
            if day not in daily_seen:
                daily_seen.add(day)
                keep.add(snapshot)
                continue
        deleted.append({"file": snapshot.name, "bytes": snapshot.stat().st_size})
        if not dry_run:
            with contextlib.suppress(OSError):
                snapshot.unlink()
            manifest = manifest_path_for(snapshot)
            with contextlib.suppress(OSError):
                manifest.unlink()
    freed = sum(item["bytes"] for item in deleted)
    return {
        "kept": len(keep),
        "deleted": deleted,
        "freed_bytes": freed,
        "dry_run": dry_run,
    }


# --------------------------------------------------------------------------- 异机/私有目录
def copy_offsite_local(
    files: list[Path], offsite_dir: str | Path, *, dry_run: bool = False
) -> dict[str, Any]:
    target_dir = ensure_private_dir(offsite_dir)
    copied: list[dict[str, Any]] = []
    for source in files:
        target = target_dir / source.name
        if dry_run:
            copied.append({"file": source.name, "target": str(target), "dry_run": True})
            continue
        temp = target.with_name(target.name + ".tmp")
        shutil.copyfile(source, temp)
        if os.name != "nt":
            temp.chmod(0o600)
        atomic_replace(temp, target)
        copied.append(
            {"file": source.name, "target": str(target), "bytes": target.stat().st_size}
        )
    return {"mode": "local", "dir": str(target_dir), "files": copied, "dry_run": dry_run}


def copy_offsite_rsync(
    files: list[Path],
    target: str,
    *,
    ssh_options: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    if not target:
        raise BackupError("rsync 模式需要 --rsync-target")
    rsync = shutil.which("rsync")
    if not rsync:
        raise BackupError("未安装 rsync")
    commands: list[list[str]] = []
    for source in files:
        command = [rsync, "--archive", "--chmod=F600,D700", "--timeout=120"]
        if ssh_options:
            command.extend(["-e", f"ssh {ssh_options}"])
        if dry_run:
            command.append("--dry-run")
        command.extend([str(source), target])
        commands.append(command)
        result = __import__("subprocess").run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise BackupError(
                f"rsync 失败（{source.name}）：{result.stderr.strip() or result.stdout.strip()}"
            )
    return {
        "mode": "rsync",
        "target": target,
        "commands": commands,
        "dry_run": dry_run,
    }


# --------------------------------------------------------------------------- 快照流程
@dataclass
class SnapshotOptions:
    source: str
    backup_root: str
    key_file: str
    recent_hours: int = 24
    daily_days: int = 30
    compress: bool = True
    offsite: str = "none"
    offsite_dir: str = ""
    rsync_target: str = ""
    rsync_ssh_options: str = ""
    dry_run: bool = False
    lock_file: str = ""
    retention_payload_days: int = 7
    retention_dedup_days: int = 30
    extra: dict[str, Any] = field(default_factory=dict)


def run_snapshot(options: SnapshotOptions) -> dict[str, Any]:
    source = Path(options.source)
    root = ensure_private_dir(options.backup_root)
    key_file = Path(options.key_file)
    read_key_file(key_file)  # 提前校验密钥，避免快照后失败
    lock_path = Path(options.lock_file) if options.lock_file else root / ".backup.lock"
    with SnapshotLock(lock_path):
        created = utc_now()
        stamp = created.strftime("%Y%m%dT%H%M%SZ")
        temp_dir = Path(tempfile.mkdtemp(prefix=f".snapshot-{stamp}-", dir=str(root)))
        try:
            snapshot_db = temp_dir / "gateway.db"
            stats = create_snapshot_db(source, snapshot_db)
            compress = Path(temp_dir / "gateway.db.gz")
            if options.compress:
                compress_file(snapshot_db, compress)
            else:
                shutil.copyfile(snapshot_db, compress)
            encrypted = root / f"gateway-{stamp}.db.enc"
            encrypt_file(compress, encrypted, key_file)
            manifest_path = root / f"gateway-{stamp}.json"
            encrypted_sha = sha256_file(encrypted)
            manifest = {
                "format": "erp-gateway-sqlite-backup",
                "tool_version": TOOL_VERSION,
                "created_at": iso_z(created),
                "snapshot": encrypted.name,
                "encrypted_sha256": encrypted_sha,
                "encrypted_bytes": encrypted.stat().st_size,
                "compressed": bool(options.compress),
                "key_file": str(key_file),
                "database": {
                    "source": str(source),
                    "db_sha256": sha256_file(snapshot_db),
                    "db_bytes": snapshot_db.stat().st_size,
                    **stats,
                },
                "retention": {
                    "recent_hours": options.recent_hours,
                    "daily_days": options.daily_days,
                    "webhook_payload_days": options.retention_payload_days,
                    "webhook_dedup_days": options.retention_dedup_days,
                    "terminal_statuses": list(TERMINAL_STATUSES),
                },
                "offsite": {"mode": options.offsite, "status": "pending"},
            }
            write_json_atomic(manifest_path, manifest)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        # 先发布加密文件与 manifest，再清理/异地；任何一步失败都不删除本地快照。
        offsite_result: dict[str, Any] = {"mode": options.offsite, "status": "skipped"}
        if options.offsite != "none":
            try:
                if options.offsite == "local":
                    offsite_result = {
                        **copy_offsite_local(
                            [encrypted, manifest_path],
                            options.offsite_dir or str(root / "offsite"),
                            dry_run=options.dry_run,
                        ),
                        "status": "ok",
                    }
                    offsite_result["pruned"] = prune_snapshots(
                        options.offsite_dir or str(root / "offsite"),
                        recent_hours=options.recent_hours,
                        daily_days=options.daily_days,
                        dry_run=True if options.dry_run else False,
                    )
                elif options.offsite == "rsync":
                    offsite_result = {
                        **copy_offsite_rsync(
                            [encrypted, manifest_path],
                            options.rsync_target,
                            ssh_options=options.rsync_ssh_options,
                            dry_run=options.dry_run,
                        ),
                        "status": "ok",
                    }
                else:
                    raise BackupError(f"未知 offsite 模式：{options.offsite}")
            except Exception as exc:  # 本地成功，异地失败需告警/非零
                offsite_result = {
                    "mode": options.offsite,
                    "status": "failed",
                    "error": str(exc),
                }
                manifest["offsite"] = offsite_result
                write_json_atomic(manifest_path, manifest)
                raise BackupError(f"异机备份失败：{exc}") from exc
        manifest["offsite"] = offsite_result
        write_json_atomic(manifest_path, manifest)

        pruned = prune_snapshots(
            root,
            recent_hours=options.recent_hours,
            daily_days=options.daily_days,
            dry_run=options.dry_run,
        )
        latest_encrypted = encrypted.stat().st_size
        budget = estimate_disk_budget(
            latest_encrypted,
            recent_hours=options.recent_hours,
            daily_days=options.daily_days,
            offsite_copies=2 if options.offsite != "none" else 1,
        )
        return {
            "ok": True,
            "snapshot": str(encrypted),
            "manifest": str(manifest_path),
            "created_at": manifest["created_at"],
            "encrypted_bytes": latest_encrypted,
            "encrypted_sha256": encrypted_sha,
            "db_bytes": manifest["database"]["db_bytes"],
            "webhook": manifest["database"]["webhook"],
            "offsite": offsite_result,
            "pruned": pruned,
            "disk_budget": budget,
        }


# --------------------------------------------------------------------------- 恢复演练
@dataclass
class DrillResult:
    ok: bool
    backup: str
    report_path: str
    checks: list[dict[str, Any]]
    manifest: dict[str, Any]
    restored_counts: dict[str, int]
    claimable_pending: int
    claimed: int
    error: str = ""


def run_restore_drill(
    *,
    backup: str,
    key_file: str,
    work_dir: str = "",
    report_dir: str = "",
    keep_workdir: bool = False,
    company: str = "",
) -> DrillResult:
    backup_path = Path(backup)
    manifest_path = manifest_path_for(backup_path)
    if not backup_path.is_file():
        raise BackupError(f"备份文件不存在：{backup_path}")
    if not manifest_path.is_file():
        raise BackupError(f"备份 manifest 不存在：{manifest_path}")
    manifest = read_json(manifest_path)
    checks: list[dict[str, Any]] = []
    work_root = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="gateway-drill-"))
    work_root.mkdir(parents=True, exist_ok=True)
    report_path: Path | None = None
    claimable_pending = 0
    claimed = 0
    try:
        actual_sha = sha256_file(backup_path)
        expected_sha = str(manifest.get("encrypted_sha256") or "")
        checks.append(
            {
                "name": "encrypted_sha256",
                "ok": bool(expected_sha) and hmac.compare_digest(actual_sha, expected_sha),
                "expected": expected_sha,
                "actual": actual_sha,
            }
        )
        if not checks[-1]["ok"]:
            raise BackupError("加密备份 SHA-256 与 manifest 不一致（文件损坏或被篡改）")
        decrypted_gz = work_root / "restored.db.gz"
        restored_db = work_root / "restored.db"
        decrypt_file(backup_path, decrypted_gz, key_file)
        with gzip.open(decrypted_gz, "rb") as src, open(restored_db, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        fsync_path(restored_db)
        checks.append({"name": "decrypt", "ok": True, "bytes": restored_db.stat().st_size})
        restored_sha = sha256_file(restored_db)
        expected_db_sha = str(manifest.get("database", {}).get("db_sha256") or "")
        checks.append(
            {
                "name": "database_sha256",
                "ok": bool(expected_db_sha) and hmac.compare_digest(restored_sha, expected_db_sha),
                "expected": expected_db_sha,
                "actual": restored_sha,
            }
        )
        if not checks[-1]["ok"]:
            raise BackupError("恢复后的数据库 SHA-256 与 manifest 不一致")
        connection, stats = validate_snapshot(restored_db)
        try:
            checks.append({"name": "integrity_check", "ok": True})
            expected_schema = str(manifest.get("database", {}).get("schema_sha256") or "")
            checks.append(
                {
                    "name": "schema_sha256",
                    "ok": expected_schema == stats["schema_sha256"],
                    "expected": expected_schema,
                    "actual": stats["schema_sha256"],
                }
            )
            expected_counts = manifest.get("database", {}).get("tables") or {}
            count_ok = all(
                int(stats["tables"].get(name, -1)) == int(count)
                for name, count in expected_counts.items()
            )
            checks.append(
                {
                    "name": "table_counts",
                    "ok": count_ok,
                    "expected": expected_counts,
                    "actual": stats["tables"],
                }
            )
            claimable = 0
            claimed = 0
            if table_exists(connection, DEFAULT_WEBHOOK_TABLE):
                row = connection.execute(
                    f"SELECT COUNT(*) FROM {DEFAULT_WEBHOOK_TABLE} WHERE status='pending'"
                ).fetchone()
                claimable = int(row[0] or 0)
                claimable_pending = claimable
                # 只读连接不能更新；另开可写临时副本模拟 dispatcher 领取。
                claim_conn = sqlite3.connect(str(restored_db), timeout=10, isolation_level=None)
                try:
                    claim_conn.execute("PRAGMA busy_timeout = 10000")
                    claim_conn.execute("BEGIN IMMEDIATE")
                    candidate = claim_conn.execute(
                        f"SELECT id FROM {DEFAULT_WEBHOOK_TABLE} WHERE status='pending' "
                        "AND (lease_expires_at IS NULL OR lease_expires_at < ?) "
                        "ORDER BY id LIMIT 1",
                        (int(time.time()),),
                    ).fetchone()
                    if candidate is not None:
                        claimed = int(
                            claim_conn.execute(
                                f"UPDATE {DEFAULT_WEBHOOK_TABLE} SET lease_token='drill-lease', "
                                "lease_expires_at=? WHERE id=? AND status='pending'",
                                (int(time.time()) + 120, int(candidate[0])),
                            ).rowcount
                            or 0
                        )
                    claim_conn.execute("ROLLBACK")
                finally:
                    claim_conn.close()
            checks.append(
                {
                    "name": "dispatcher_claim_simulation",
                    "ok": claimed >= 1 or claimable == 0,
                    "claimable_pending": claimable,
                    "claimed": claimed,
                    "note": "事务内模拟领取后回滚，不修改备份文件",
                }
            )
        finally:
            connection.close()
        failed = [item for item in checks if not item.get("ok")]
        report = {
            "format": "erp-gateway-restore-drill",
            "tool_version": TOOL_VERSION,
            "created_at": iso_z(),
            "backup": str(backup_path),
            "backup_created_at": manifest.get("created_at"),
            "manifest": manifest,
            "checks": checks,
            "ok": not failed,
            "restored_counts": (manifest.get("database", {}).get("tables") or {}),
            "claimable_pending": claimable_pending,
            "claimed": claimed,
            "work_dir": str(work_root),
        }
        report_dir_path = Path(report_dir) if report_dir else work_root
        ensure_private_dir(report_dir_path)
        report_path = report_dir_path / f"drill-report-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.json"
        write_json_atomic(report_path, report)
        if failed:
            raise BackupError("恢复演练校验失败：" + ", ".join(str(item["name"]) for item in failed))
        return DrillResult(
            ok=True,
            backup=str(backup_path),
            report_path=str(report_path),
            checks=checks,
            manifest=manifest,
            restored_counts=report["restored_counts"],
            claimable_pending=int(report["claimable_pending"]),
            claimed=int(report["claimed"]),
        )
    finally:
        if report_path is None and not keep_workdir:
            shutil.rmtree(work_root, ignore_errors=True)


# --------------------------------------------------------------------------- 保留清理
def run_retention(
    *,
    database: str,
    payload_days: int = 7,
    dedup_days: int = 30,
    apply: bool = False,
) -> dict[str, Any]:
    """已完成事件原文/行保留策略；未完成事件始终不删除。"""
    now = utc_now()
    payload_cutoff = iso_z(now - timedelta(days=max(0, int(payload_days))))
    dedup_cutoff = iso_z(now - timedelta(days=max(0, int(dedup_days))))
    connection = connect_readonly(database) if not apply else connect_live(database)
    try:
        if not table_exists(connection, DEFAULT_WEBHOOK_TABLE):
            return {
                "ok": True,
                "available": False,
                "message": "gateway_webhook_events 不存在，跳过",
                "dry_run": not apply,
            }
        columns = table_columns(connection, DEFAULT_WEBHOOK_TABLE)
        payload_expr = ""
        if "payload_cipher" in columns:
            payload_expr = (
                "LENGTH(payload_cipher) + COALESCE(LENGTH(payload_sha256), 0)"
            )
        before = {
            "total": int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {DEFAULT_WEBHOOK_TABLE}"
                ).fetchone()[0]
            ),
        }
        status_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                f"SELECT status, COUNT(*) FROM {DEFAULT_WEBHOOK_TABLE} GROUP BY status"
            ).fetchall()
        }
        terminal = "','".join(TERMINAL_STATUSES)
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        payload_bytes_before = 0
        if payload_expr:
            payload_bytes_before = int(
                connection.execute(
                    f"SELECT COALESCE(SUM({payload_expr}),0) FROM {DEFAULT_WEBHOOK_TABLE}"
                ).fetchone()[0]
                or 0
            )
        scrub_condition = (
            f"status IN ({placeholders}) AND created_at < ?"
            if payload_expr
            else "0"
        )
        scrub_sql = ""
        if payload_expr:
            assignments = "payload_cipher = ''"
            if "payload_sha256" in columns:
                assignments += ", payload_sha256 = ''"
            scrub_sql = (
                f"UPDATE {DEFAULT_WEBHOOK_TABLE} SET {assignments} "
                f"WHERE {scrub_condition} AND ({payload_expr}) > 0"
            )
        delete_sql = (
            f"DELETE FROM {DEFAULT_WEBHOOK_TABLE} "
            f"WHERE status IN ({placeholders}) AND created_at < ?"
        )
        if not apply:
            # dry-run 使用只读连接 + 纯 SELECT，避免每 15 分钟 sidecar 抢写锁。
            scrub_count = 0
            scrub_bytes = 0
            if payload_expr:
                scrub_count = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {DEFAULT_WEBHOOK_TABLE} "
                        f"WHERE {scrub_condition} AND ({payload_expr}) > 0",
                        (*TERMINAL_STATUSES, payload_cutoff),
                    ).fetchone()[0]
                    or 0
                )
                scrub_bytes = int(
                    connection.execute(
                        f"SELECT COALESCE(SUM({payload_expr}),0) FROM {DEFAULT_WEBHOOK_TABLE} "
                        f"WHERE {scrub_condition} AND ({payload_expr}) > 0",
                        (*TERMINAL_STATUSES, payload_cutoff),
                    ).fetchone()[0]
                    or 0
                )
            delete_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {DEFAULT_WEBHOOK_TABLE} "
                    f"WHERE status IN ({placeholders}) AND created_at < ?",
                    (*TERMINAL_STATUSES, dedup_cutoff),
                ).fetchone()[0]
                or 0
            )
            deleted_by_status = {
                str(row[0]): int(row[1] or 0)
                for row in connection.execute(
                    f"SELECT status, COUNT(*) FROM {DEFAULT_WEBHOOK_TABLE} "
                    f"WHERE status IN ({placeholders}) AND created_at < ? GROUP BY status",
                    (*TERMINAL_STATUSES, dedup_cutoff),
                ).fetchall()
            }
            projected = dict(status_counts)
            for status, count in deleted_by_status.items():
                projected[status] = max(0, projected.get(status, 0) - count)
            protected_incomplete = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {DEFAULT_WEBHOOK_TABLE} "
                    "WHERE status NOT IN ('delivered','dead','unroutable')"
                ).fetchone()[0]
            )
            return {
                "ok": True,
                "available": True,
                "dry_run": True,
                "readonly": True,
                "payload_days": payload_days,
                "dedup_days": dedup_days,
                "payload_cutoff": payload_cutoff,
                "dedup_cutoff": dedup_cutoff,
                "before_total": before["total"],
                "after_total": before["total"],
                "status_counts_before": status_counts,
                "status_counts_after": projected,
                "scrubbed_rows": 0,
                "would_scrub_rows": scrub_count,
                "deleted_rows": 0,
                "would_delete_rows": delete_count,
                "payload_bytes_before": payload_bytes_before,
                "payload_bytes_after": max(0, payload_bytes_before - scrub_bytes),
                "protected_incomplete": protected_incomplete,
                "note": "dry-run 只读：未加写锁、未删除；--apply 才执行（未完成事件永不删除）",
            }
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            scrubbed = 0
            deleted = 0
            if scrub_sql:
                scrubbed = int(
                    connection.execute(
                        scrub_sql, (*TERMINAL_STATUSES, payload_cutoff)
                    ).rowcount
                    or 0
                )
            deleted = int(
                connection.execute(
                    delete_sql, (*TERMINAL_STATUSES, dedup_cutoff)
                ).rowcount
                or 0
            )
            payload_bytes_after = 0
            if payload_expr:
                payload_bytes_after = int(
                    connection.execute(
                        f"SELECT COALESCE(SUM({payload_expr}),0) FROM {DEFAULT_WEBHOOK_TABLE}"
                    ).fetchone()[0]
                    or 0
                )
            after_total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {DEFAULT_WEBHOOK_TABLE}"
                ).fetchone()[0]
            )
            protected_incomplete = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {DEFAULT_WEBHOOK_TABLE} "
                    "WHERE status NOT IN ('delivered','dead','unroutable')"
                ).fetchone()[0]
            )
            status_after = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    f"SELECT status, COUNT(*) FROM {DEFAULT_WEBHOOK_TABLE} GROUP BY status"
                ).fetchall()
            }
            if not apply:
                connection.execute("ROLLBACK")
            else:
                connection.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        return {
            "ok": True,
            "available": True,
            "dry_run": not apply,
            "payload_days": payload_days,
            "dedup_days": dedup_days,
            "payload_cutoff": payload_cutoff,
            "dedup_cutoff": dedup_cutoff,
            "before_total": before["total"],
            "after_total": after_total if apply else before["total"],
            "status_counts_before": status_counts,
            "status_counts_after": status_after if apply else status_counts,
            "scrubbed_rows": scrubbed if apply else 0,
            "would_scrub_rows": scrubbed,
            "deleted_rows": deleted if apply else 0,
            "would_delete_rows": deleted,
            "payload_bytes_before": payload_bytes_before,
            "payload_bytes_after": payload_bytes_after if apply else payload_bytes_before,
            "protected_incomplete": protected_incomplete,
            "note": "未完成事件（pending/retry_wait/processing 等）永不删除；--apply 才执行",
        }
    finally:
        connection.close()


# --------------------------------------------------------------------------- 状态/预算
def read_latest_manifest(root: str | Path) -> dict[str, Any] | None:
    directory = Path(root)
    if not directory.is_dir():
        return None
    manifests = [
        path
        for path in directory.iterdir()
        if MANIFEST_RE.match(path.name)
    ]
    manifests.sort(key=lambda item: item.name, reverse=True)
    for path in manifests:
        try:
            return read_json(path)
        except (OSError, ValueError):
            continue
    return None


def estimate_disk_budget(
    encrypted_bytes: int,
    *,
    recent_hours: int,
    daily_days: int,
    offsite_copies: int = 1,
    overhead_ratio: float = 0.10,
) -> dict[str, Any]:
    recent_count = max(0, int(recent_hours)) * 4  # 每 15 分钟一份
    daily_count = max(0, int(daily_days))
    copies = max(1, int(offsite_copies))
    per_snapshot = max(0, int(encrypted_bytes))
    raw = per_snapshot * (recent_count + daily_count) * copies
    total = int(raw * (1 + max(0.0, overhead_ratio))) + 64 * 1024 * 1024
    return {
        "encrypted_snapshot_bytes": per_snapshot,
        "recent_snapshots": recent_count,
        "daily_snapshots": daily_count,
        "offsite_copies": copies,
        "overhead_ratio": overhead_ratio,
        "overhead_bytes": 64 * 1024 * 1024,
        "recommended_disk_budget_bytes": total,
        "recommended_disk_budget_gib": round(total / (1024**3), 2),
        "formula": "encrypted_bytes × (recent_hours×4 + daily_days) × copies × (1+overhead) + 64MiB",
    }


def collect_status(
    *,
    database: str,
    backup_root: str,
    recent_hours: int = 24,
    daily_days: int = 30,
    disk_quota_bytes: int = 0,
) -> dict[str, Any]:
    db_path = Path(database)
    live_stats: dict[str, Any] = {}
    if db_path.is_file():
        try:
            connection = connect_readonly(db_path)
            try:
                live_stats = database_stats(connection)
            finally:
                connection.close()
        except Exception as exc:  # 生产只读探测失败不应让巡检崩溃
            live_stats = {"error": str(exc)}
    snapshots = list_snapshots(backup_root)
    manifests: list[dict[str, Any]] = []
    for snapshot in snapshots:
        manifest = manifest_path_for(snapshot)
        if manifest.is_file():
            with contextlib.suppress(OSError, ValueError):
                manifests.append(read_json(manifest))
    latest = manifests[0] if manifests else None
    latest_size = (
        int(latest.get("encrypted_bytes") or 0) if latest else (snapshots[0].stat().st_size if snapshots else 0)
    )
    used_bytes = sum(path.stat().st_size for path in Path(backup_root).glob("*") if path.is_file()) if Path(backup_root).is_dir() else 0
    budget = estimate_disk_budget(
        latest_size,
        recent_hours=recent_hours,
        daily_days=daily_days,
        offsite_copies=1,
    )
    quota = int(disk_quota_bytes or budget["recommended_disk_budget_bytes"])
    growth_per_hour = None
    samples = [
        item for item in manifests if item.get("database", {}).get("db_bytes") is not None
    ]
    if len(samples) >= 2:
        try:
            newest = samples[0]
            oldest = samples[-1]
            newest_at = parse_iso(str(newest["created_at"]))
            oldest_at = parse_iso(str(oldest["created_at"]))
            hours = max(0.01, (newest_at - oldest_at).total_seconds() / 3600)
            delta = int(newest["database"]["db_bytes"]) - int(oldest["database"]["db_bytes"])
            growth_per_hour = delta / hours
        except (KeyError, ValueError, TypeError):
            growth_per_hour = None
    hours_left = None
    if growth_per_hour and growth_per_hour > 0:
        hours_left = round(max(0.0, (quota - used_bytes) / growth_per_hour), 1)
    webhook = live_stats.get("webhook") or {}
    return {
        "ok": True,
        "checked_at": iso_z(),
        "database": {
            "path": str(db_path),
            "exists": db_path.is_file(),
            "size_bytes": db_path.stat().st_size if db_path.is_file() else 0,
            "stats": live_stats,
        },
        "webhook_pending": webhook.get("pending"),
        "oldest_pending_age_seconds": webhook.get("oldest_pending_age_seconds"),
        "snapshot": {
            "count": len(snapshots),
            "latest": latest.get("snapshot") if latest else None,
            "latest_created_at": latest.get("created_at") if latest else None,
            "latest_encrypted_bytes": latest_size,
            "backup_root": str(backup_root),
            "used_bytes": used_bytes,
        },
        "disk": {
            "used_bytes": used_bytes,
            "quota_bytes": quota,
            "budget": budget,
            "growth_bytes_per_hour": growth_per_hour,
            "acceptable_hours_remaining": hours_left,
        },
    }


# --------------------------------------------------------------------------- 自测
def _create_test_database(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE gateway_instances (id INTEGER PRIMARY KEY, tenant_id INTEGER, status TEXT);
            CREATE TABLE gateway_channel_apps (channel TEXT, environment TEXT, secret_cipher TEXT);
            CREATE TABLE gateway_oauth_states (state_hash TEXT PRIMARY KEY, expires_at INTEGER);
            CREATE TABLE gateway_refresh_locks (lock_key TEXT PRIMARY KEY, expires_at INTEGER);
            CREATE TABLE gateway_deliveries (id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE gateway_rate_limits (scope TEXT PRIMARY KEY, count INTEGER);
            CREATE TABLE gateway_settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE gateway_audit_logs (id INTEGER PRIMARY KEY, action TEXT);
            CREATE TABLE gateway_webhook_shops (id INTEGER PRIMARY KEY);
            CREATE TABLE gateway_webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                notification_id TEXT NOT NULL,
                payload_cipher TEXT NOT NULL DEFAULT '',
                payload_sha256 TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                lease_token TEXT NOT NULL DEFAULT '',
                lease_expires_at INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(channel, notification_id)
            );
            """
        )
        connection.execute(
            "INSERT INTO gateway_instances(id, tenant_id, status) VALUES (1, 3, 'active')"
        )
        connection.execute(
            "INSERT INTO gateway_channel_apps VALUES ('tiktok','production','cipher')"
        )
        now = utc_now()
        rows = [
            ("tiktok", "pending-old", "pending", "", now - timedelta(days=10)),
            ("tiktok", "delivered-old", "delivered", "x" * 500, now - timedelta(days=8)),
            ("tiktok", "delivered-expired", "delivered", "y" * 500, now - timedelta(days=31)),
            ("tiktok", "delivered-recent", "delivered", "z" * 500, now - timedelta(hours=1)),
            ("tiktok", "dead-old", "dead", "d" * 500, now - timedelta(days=8)),
            ("tiktok", "unroutable-expired", "unroutable", "u" * 500, now - timedelta(days=40)),
        ]
        for channel, notification_id, status, payload, created in rows:
            connection.execute(
                """INSERT INTO gateway_webhook_events
                   (channel, notification_id, payload_cipher, payload_sha256, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    channel,
                    notification_id,
                    payload,
                    sha256_text(payload) if payload else "",
                    status,
                    iso_z(created),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def run_self_test() -> dict[str, Any]:
    temp_root = Path(tempfile.mkdtemp(prefix="gateway-backup-selftest-"))
    results: list[dict[str, Any]] = []
    try:
        source = temp_root / "gateway.db"
        _create_test_database(source)
        key_file = temp_root / "backup.key"
        init_key_file(key_file)
        backup_root = ensure_private_dir(temp_root / "backups")
        offsite = ensure_private_dir(temp_root / "offsite")

        # 1) 并发写入期间做在线备份，验证一致性（不是 cp WAL）。
        stop = threading.Event()
        errors: list[str] = []

        def writer() -> None:
            connection = sqlite3.connect(str(source), timeout=10)
            try:
                for index in range(80):
                    if stop.is_set():
                        break
                    connection.execute(
                        """INSERT INTO gateway_webhook_events
                           (channel, notification_id, payload_cipher, status, created_at)
                           VALUES ('tiktok', ?, 'live-payload', 'pending', ?)""",
                        (f"concurrent-{index}", iso_z()),
                    )
                    connection.commit()
                    time.sleep(0.003)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
            finally:
                connection.close()

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        time.sleep(0.02)
        snapshot_result = run_snapshot(
            SnapshotOptions(
                source=str(source),
                backup_root=str(backup_root),
                key_file=str(key_file),
                recent_hours=24,
                daily_days=30,
                offsite="local",
                offsite_dir=str(offsite),
            )
        )
        stop.set()
        thread.join(timeout=10)
        results.append(
            {
                "name": "consistent_online_snapshot",
                "ok": snapshot_result["ok"] and not errors,
                "encrypted_bytes": snapshot_result["encrypted_bytes"],
                "db_bytes": snapshot_result["db_bytes"],
                "writer_errors": errors,
            }
        )
        if not snapshot_result["ok"]:
            raise BackupError("自测快照失败")

        # 2) 篡改检测：修改加密文件必须被恢复演练拒绝。
        tampered = temp_root / "tampered.db.enc"
        shutil.copyfile(snapshot_result["snapshot"], tampered)
        shutil.copyfile(snapshot_result["manifest"], temp_root / "tampered.json")
        with open(tampered, "r+b") as handle:
            handle.seek(64)
            original = handle.read(1)
            handle.seek(64)
            handle.write(bytes([original[0] ^ 0xFF]))
        tamper_failed = False
        try:
            run_restore_drill(
                backup=str(tampered),
                key_file=str(key_file),
                work_dir=str(temp_root / "drill-tamper"),
                report_dir=str(temp_root / "drill-tamper-reports"),
            )
        except BackupError as exc:
            tamper_failed = "SHA-256" in str(exc) or "HMAC" in str(exc) or "篡改" in str(exc)
        results.append({"name": "tamper_detection", "ok": tamper_failed})

        # 3) 正常恢复演练：完整性/schema/行数/模拟 dispatcher 领取。
        drill = run_restore_drill(
            backup=snapshot_result["snapshot"],
            key_file=str(key_file),
            work_dir=str(temp_root / "drill"),
            report_dir=str(temp_root / "drill-reports"),
        )
        results.append(
            {
                "name": "restore_drill",
                "ok": drill.ok and drill.claimed >= 1,
                "claimed": drill.claimed,
                "claimable_pending": drill.claimable_pending,
                "report": drill.report_path,
            }
        )

        # 4) 保留策略：未完成保留、已完成原文 7 天、去重行 30 天；只删已投递过期。
        retention_dry = run_retention(database=str(source), apply=False)
        retention_apply = run_retention(database=str(source), apply=True)
        connection = sqlite3.connect(str(source))
        try:
            pending_row = connection.execute(
                "SELECT payload_cipher FROM gateway_webhook_events WHERE notification_id='pending-old'"
            ).fetchone()
            old_payload = connection.execute(
                "SELECT payload_cipher FROM gateway_webhook_events WHERE notification_id='delivered-old'"
            ).fetchone()
            expired = connection.execute(
                "SELECT COUNT(*) FROM gateway_webhook_events WHERE notification_id='delivered-expired'"
            ).fetchone()[0]
            recent = connection.execute(
                "SELECT payload_cipher FROM gateway_webhook_events WHERE notification_id='delivered-recent'"
            ).fetchone()
        finally:
            connection.close()
        retention_ok = (
            pending_row is not None
            and pending_row[0] == ""
            and old_payload is not None
            and old_payload[0] == ""
            and int(expired) == 0
            and recent is not None
            and recent[0] != ""
            and retention_apply["deleted_rows"] >= 2
            and retention_apply["scrubbed_rows"] >= 1
        )
        results.append(
            {
                "name": "retention_policy",
                "ok": retention_ok,
                "dry_run": retention_dry,
                "apply": retention_apply,
            }
        )

        # 5) 磁盘预算/状态输出可计算。
        budget = estimate_disk_budget(
            snapshot_result["encrypted_bytes"], recent_hours=24, daily_days=30
        )
        status = collect_status(
            database=str(source),
            backup_root=str(backup_root),
            disk_quota_bytes=budget["recommended_disk_budget_bytes"],
        )
        results.append(
            {
                "name": "disk_budget_and_status",
                "ok": budget["recommended_disk_budget_bytes"] > 0
                and status["snapshot"]["count"] >= 1,
                "budget": budget,
                "pending": status.get("webhook_pending"),
            }
        )
        passed = all(item["ok"] for item in results)
        return {
            "ok": passed,
            "temp_root": str(temp_root),
            "checks": results,
            "snapshot": snapshot_result,
        }
    finally:
        # 自测临时目录保留由调用方决定：默认清理，避免堆积。
        shutil.rmtree(temp_root, ignore_errors=True)


# --------------------------------------------------------------------------- CLI
def _print(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, ensure_ascii=False, default=str)}")
        else:
            print(f"{key}: {value}")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ERP 网关 SQLite 快照/加密备份/恢复演练/保留策略（task-45）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"gateway_backup {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot", help="一致性快照 + 加密 + 异地复制 + 保留清理")
    snapshot.add_argument("--source", default=_env("GATEWAY_DB_PATH", "/data/gateway.db"))
    snapshot.add_argument("--backup-root", default=_env("GATEWAY_BACKUP_ROOT", "/var/lib/erp-gateway-backups"))
    snapshot.add_argument("--key-file", default=_env("GATEWAY_BACKUP_KEY_FILE", "/etc/erp-gateway/backup.key"))
    snapshot.add_argument("--recent-hours", type=int, default=int(_env("GATEWAY_BACKUP_RECENT_HOURS", "24")))
    snapshot.add_argument("--daily-days", type=int, default=int(_env("GATEWAY_BACKUP_DAILY_DAYS", "30")))
    snapshot.add_argument("--offsite", choices=["none", "local", "rsync"], default=_env("GATEWAY_BACKUP_OFFSITE_MODE", "none"))
    snapshot.add_argument("--offsite-dir", default=_env("GATEWAY_BACKUP_OFFSITE_DIR", ""))
    snapshot.add_argument("--rsync-target", default=_env("GATEWAY_BACKUP_RSYNC_TARGET", ""))
    snapshot.add_argument("--rsync-ssh-options", default=_env("GATEWAY_BACKUP_RSYNC_SSH_OPTIONS", ""))
    snapshot.add_argument("--lock-file", default=_env("GATEWAY_BACKUP_LOCK_FILE", ""))
    snapshot.add_argument("--dry-run", action="store_true")
    snapshot.add_argument("--json", action="store_true")
    snapshot.add_argument("--init-key", action="store_true", help="仅生成密钥文件后退出")
    snapshot.add_argument("--payload-days", type=int, default=int(_env("GATEWAY_RETENTION_PAYLOAD_DAYS", "7")))
    snapshot.add_argument("--dedup-days", type=int, default=int(_env("GATEWAY_RETENTION_DEDUP_DAYS", "30")))

    drill = sub.add_parser("restore-drill", help="在临时目录解密/校验/模拟 dispatcher 领取")
    drill.add_argument("--backup", required=True)
    drill.add_argument("--key-file", default=_env("GATEWAY_BACKUP_KEY_FILE", "/etc/erp-gateway/backup.key"))
    drill.add_argument("--work-dir", default="")
    drill.add_argument("--report-dir", default="")
    drill.add_argument("--keep-workdir", action="store_true")
    drill.add_argument("--json", action="store_true")

    retention = sub.add_parser("retention", help="清理已完成事件原文/去重记录（默认 dry-run）")
    retention.add_argument("--database", default=_env("GATEWAY_DB_PATH", "/data/gateway.db"))
    retention.add_argument("--payload-days", type=int, default=int(_env("GATEWAY_RETENTION_PAYLOAD_DAYS", "7")))
    retention.add_argument("--dedup-days", type=int, default=int(_env("GATEWAY_RETENTION_DEDUP_DAYS", "30")))
    retention.add_argument("--apply", action="store_true")
    retention.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="只读展示 pending/磁盘/增长率/最近快照")
    status.add_argument("--database", default=_env("GATEWAY_DB_PATH", "/data/gateway.db"))
    status.add_argument("--backup-root", default=_env("GATEWAY_BACKUP_ROOT", "/var/lib/erp-gateway-backups"))
    status.add_argument("--recent-hours", type=int, default=int(_env("GATEWAY_BACKUP_RECENT_HOURS", "24")))
    status.add_argument("--daily-days", type=int, default=int(_env("GATEWAY_BACKUP_DAILY_DAYS", "30")))
    status.add_argument("--disk-quota-bytes", type=int, default=int(_env("GATEWAY_BACKUP_DISK_QUOTA_BYTES", "0")))
    status.add_argument("--json", action="store_true")

    budget = sub.add_parser("disk-budget", help="输出磁盘预算（按最新快照大小/保留层数估算）")
    budget.add_argument("--database", default=_env("GATEWAY_DB_PATH", "/data/gateway.db"))
    budget.add_argument("--backup-root", default=_env("GATEWAY_BACKUP_ROOT", "/var/lib/erp-gateway-backups"))
    budget.add_argument("--recent-hours", type=int, default=int(_env("GATEWAY_BACKUP_RECENT_HOURS", "24")))
    budget.add_argument("--daily-days", type=int, default=int(_env("GATEWAY_BACKUP_DAILY_DAYS", "30")))
    budget.add_argument("--offsite-copies", type=int, default=int(_env("GATEWAY_BACKUP_OFFSITE_COPIES", "1")))
    budget.add_argument("--json", action="store_true")

    sub.add_parser("self-test", help="临时 SQLite 端到端自测")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            if args.init_key:
                init_key_file(args.key_file)
                print(f"已生成备份密钥：{args.key_file}（权限 600，请立即备份到独立位置）")
                return 0
            result = run_snapshot(
                SnapshotOptions(
                    source=args.source,
                    backup_root=args.backup_root,
                    key_file=args.key_file,
                    recent_hours=args.recent_hours,
                    daily_days=args.daily_days,
                    offsite=args.offsite,
                    offsite_dir=args.offsite_dir,
                    rsync_target=args.rsync_target,
                    rsync_ssh_options=args.rsync_ssh_options,
                    dry_run=args.dry_run,
                    lock_file=args.lock_file,
                    retention_payload_days=args.payload_days,
                    retention_dedup_days=args.dedup_days,
                )
            )
            _print(result, args.json)
            return 0
        if args.command == "restore-drill":
            result = run_restore_drill(
                backup=args.backup,
                key_file=args.key_file,
                work_dir=args.work_dir,
                report_dir=args.report_dir,
                keep_workdir=args.keep_workdir,
            )
            _print(
                {
                    "ok": result.ok,
                    "backup": result.backup,
                    "report_path": result.report_path,
                    "claimable_pending": result.claimable_pending,
                    "claimed": result.claimed,
                    "checks": result.checks,
                },
                args.json,
            )
            return 0
        if args.command == "retention":
            result = run_retention(
                database=args.database,
                payload_days=args.payload_days,
                dedup_days=args.dedup_days,
                apply=args.apply,
            )
            _print(result, args.json)
            return 0 if result.get("ok") else 1
        if args.command == "status":
            result = collect_status(
                database=args.database,
                backup_root=args.backup_root,
                recent_hours=args.recent_hours,
                daily_days=args.daily_days,
                disk_quota_bytes=args.disk_quota_bytes,
            )
            _print(result, args.json)
            return 0
        if args.command == "disk-budget":
            latest = read_latest_manifest(args.backup_root)
            root = Path(args.backup_root)
            latest_size = 0
            if latest:
                latest_size = int(latest.get("encrypted_bytes") or 0)
            elif root.is_dir():
                snapshots = list_snapshots(root)
                if snapshots:
                    latest_size = snapshots[0].stat().st_size
            db_size = Path(args.database).stat().st_size if Path(args.database).is_file() else 0
            budget = estimate_disk_budget(
                latest_size or max(1, db_size // 2),
                recent_hours=args.recent_hours,
                daily_days=args.daily_days,
                offsite_copies=args.offsite_copies,
            )
            budget["source_db_bytes"] = db_size
            budget["latest_encrypted_bytes"] = latest_size
            _print(budget, args.json)
            return 0
        if args.command == "self-test":
            result = run_self_test()
            _print(result, False)
            return 0 if result.get("ok") else 1
    except AlreadyRunning as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 3
    except BackupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{exc}", "trace": traceback.format_exc()}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
