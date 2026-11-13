"""备份记录 CLI（在 api 容器内执行，供部署脚本上报备份/恢复状态）。

部署脚本（``deploy/scripts/backup.sh``、``restore_drill.sh``）在宿主机运行，不直接连
中心库；通过 ``docker compose exec -T api python -m app.modules.ops.backup_cli`` 调用本模块，
把生成的备份清单 / 恢复演练结论写入中心库 ``backup_records``。

修复要点（2026-11-13 备份记录关联 bug）：

* 旧版 Agent 备份任务只写 ``storage_key``（本地备份目录），``local_path``/``checksum`` 为空；
* 现在 ``apply`` 允许缺少 ``instance_id``：按 ``local_path / storage_key / run_id`` 精确关联
  已有记录并补齐字段，避免产生重复记录；
* ``restore-check`` 支持按 ``backup_id`` 或 ``run_id/local_path/checksum`` 精确定位唯一记录，
  找到多条或找不到时返回明确错误与候选，不误写；
* ``match`` 只读查询，用于脚本/人工核对记录。

用法::

    # 上报一次备份（JSON 从环境变量或 stdin 读取，避免命令行长度/转义问题）
    docker compose exec -T -e BACKUP_MANIFEST_JSON='{"run_id":...}' api \
        python -m app.modules.ops.backup_cli apply

    # 按 run_id/local_path/checksum 精确匹配（只读）
    docker compose exec -T -e BACKUP_MATCH_JSON='{"run_id":"...","local_path":"...","checksum":"sha256:..."}' api \
        python -m app.modules.ops.backup_cli match

    # 记录隔离恢复演练结果（可给 backup_id，也可给匹配条件）
    docker compose exec -T -e BACKUP_RESTORE_JSON='{"run_id":"...","local_path":"...","checksum":"sha256:...","status":"verified","notes":"..."}' api \
        python -m app.modules.ops.backup_cli restore-check

输出：``{"ok": true, ...}``；失败退出码 1 并输出 ``{"ok": false, "error": "..."}``。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

ENV_MANIFEST = "BACKUP_MANIFEST_JSON"
ENV_RESTORE = "BACKUP_RESTORE_JSON"
ENV_MATCH = "BACKUP_MATCH_JSON"
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


def _match_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    """从载荷提取匹配条件（纯函数，便于测试）。"""
    raw_instance = payload.get("instance_id")
    instance_id = int(raw_instance) if raw_instance not in (None, "") else None
    return {
        "instance_id": instance_id,
        "run_id": str(payload.get("run_id") or "").strip(),
        "local_path": str(payload.get("local_path") or payload.get("local_dir") or "").strip(),
        "checksum": str(payload.get("checksum") or "").strip(),
    }


def _apply() -> int:
    from app.db.session import center_session
    from app.modules.ops import service

    manifest = _read_json(ENV_MANIFEST)
    # backup.sh 在 Agent 任务里可能没有显式 --instance-id；从环境兜底（仍允许后端关联旧记录）
    if not manifest.get("instance_id"):
        env_instance = os.environ.get("BACKUP_INSTANCE_ID", "").strip()
        if env_instance.isdigit() and int(env_instance) > 0:
            manifest["instance_id"] = int(env_instance)
    with center_session() as center:
        result = service.apply_backup_manifest(center, manifest)
        center.commit()
    _emit({"ok": True, "action": "apply", "backup": result})
    return 0


def _match() -> int:
    from app.db.session import center_session
    from app.modules.ops import service

    payload = _read_json(ENV_MATCH)
    criteria = _match_kwargs(payload)
    with center_session() as center:
        matches = service.find_backup_records(center, **criteria)
    _emit(
        {
            "ok": True,
            "action": "match",
            "criteria": criteria,
            "unique": len(matches) == 1,
            "count": len(matches),
            "matches": matches,
        }
    )
    return 0


def _restore_check() -> int:
    from app.db.session import center_session
    from app.modules.ops import service

    payload = _read_json(ENV_RESTORE)
    backup_id = int(payload.get("backup_id") or payload.get("id") or 0) or None
    criteria = _match_kwargs(payload)
    if not backup_id and not any(
        (criteria["run_id"], criteria["local_path"], criteria["checksum"])
    ):
        raise ValueError("恢复演练记录缺少 backup_id 或 run_id/local_path/checksum")
    status = str(payload.get("status") or "")
    notes = str(payload.get("notes") or "")
    actor = payload.get("actor_user_id")
    with center_session() as center:
        target = service.resolve_backup_record(center, backup_id=backup_id, **criteria)
        result = service.record_restore_check(
            center,
            int(target["id"]),
            status=status,
            notes=notes,
            actor_user_id=int(actor) if actor else None,
        )
        center.commit()
    _emit(
        {
            "ok": True,
            "action": "restore-check",
            "matched_by": {"backup_id": backup_id, **criteria},
            "backup": result,
        }
    )
    return 0


def _latest() -> int:
    """输出最新一条备份记录（兼容旧脚本；新脚本建议用 run_id/local_path/checksum 精确匹配）。"""
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
        if action == "match":
            return _match()
        if action == "latest":
            return _latest()
        raise ValueError(f"未知动作：{action}（支持 apply / match / restore-check / latest）")
    except Exception as exc:  # noqa: BLE001 - CLI 需要把错误以 JSON 输出，便于脚本判断
        _emit({"ok": False, "action": action, "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":  # pragma: no cover - 容器入口
    raise SystemExit(main())
