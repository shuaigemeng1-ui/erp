"""备份记录 CLI（在 api 容器内执行，供部署脚本上报备份/恢复状态）。

部署脚本（``deploy/scripts/backup.sh``、``restore_drill.sh``）在宿主机运行，不直接连
中心库；通过 ``docker compose exec -T api python -m app.modules.ops.backup_cli`` 调用本模块，
把生成的备份清单 / 恢复演练结论写入中心库 ``backup_records``。

用法::

    # 上报一次备份（JSON 从环境变量或 stdin 读取，避免命令行长度/转义问题）
    docker compose exec -T -e BACKUP_MANIFEST_JSON='{...}' api \
        python -m app.modules.ops.backup_cli apply

    # 记录隔离恢复演练结果
    docker compose exec -T -e BACKUP_RESTORE_JSON='{"backup_id": 3, "status": "verified", "notes": "..."}' api \
        python -m app.modules.ops.backup_cli restore-check

    # 从标准输入读取（适合脚本 pipe）
    ... | docker compose exec -T api python -m app.modules.ops.backup_cli apply

输出：``{"ok": true, "backup": {...}}``；失败退出码 1 并输出 ``{"ok": false, "error": "..."}``。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

ENV_MANIFEST = "BACKUP_MANIFEST_JSON"
ENV_RESTORE = "BACKUP_RESTORE_JSON"
ENV_ACTION = "BACKUP_CLI_ACTION"


def _read_json(env_name: str) -> dict[str, Any]:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        # 允许通过 stdin 传入（不阻塞：仅在非 TTY 时读取）
        if not sys.stdin.isatty():
            raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError(f"缺少 {env_name} 或标准输入 JSON")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("JSON 必须是对象")
    return data


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def _apply() -> int:
    from app.db.session import center_session
    from app.modules.ops import service

    manifest = _read_json(ENV_MANIFEST)
    with center_session() as center:
        result = service.apply_backup_manifest(center, manifest)
        center.commit()
    _emit({"ok": True, "action": "apply", "backup": result})
    return 0


def _restore_check() -> int:
    from app.db.session import center_session
    from app.modules.ops import service

    payload = _read_json(ENV_RESTORE)
    backup_id = int(payload.get("backup_id") or payload.get("id") or 0)
    if backup_id <= 0:
        raise ValueError("恢复演练记录缺少 backup_id")
    status = str(payload.get("status") or "")
    notes = str(payload.get("notes") or "")
    actor = payload.get("actor_user_id")
    with center_session() as center:
        result = service.record_restore_check(
            center,
            backup_id,
            status=status,
            notes=notes,
            actor_user_id=int(actor) if actor else None,
        )
        center.commit()
    _emit({"ok": True, "action": "restore-check", "backup": result})
    return 0


def _latest() -> int:
    """输出最新一条备份记录（供恢复演练脚本 --record-latest 使用）。"""
    from app.modules.ops import service

    instance_id = int(os.environ.get("BACKUP_INSTANCE_ID") or 0) or None
    with __import__("app.db.session", fromlist=["center_session"]).center_session() as center:
        result = service.list_backups(center, instance_id=instance_id, page=1, page_size=1)
    items = result.get("items") or []
    if not items:
        raise ValueError("没有可用的备份记录")
    _emit({"ok": True, "action": "latest", "backup": items[0]})
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    action = args[0] if args else os.environ.get(ENV_ACTION, "apply")
    try:
        if action == "apply":
            return _apply()
        if action in ("restore-check", "restore_check"):
            return _restore_check()
        if action == "latest":
            return _latest()
        raise ValueError(f"未知动作：{action}（支持 apply / restore-check / latest）")
    except Exception as exc:  # noqa: BLE001 - CLI 需要把错误以 JSON 输出，便于脚本判断
        _emit({"ok": False, "action": action, "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":  # pragma: no cover - 容器入口
    raise SystemExit(main())
