#!/usr/bin/env python3
"""第二租户迁移白名单与停写对账工具（批次 4，离线优先）。

用途
----
1. 校验 ``deploy/scripts/tenant-onboarding/migration-whitelist.json``：
   只允许白名单内的中心/租户表与列进入迁移；禁止平台密钥类表/列；
   ``id`` / ``tenant_id`` / ``member_no`` / ``source_user_id`` 等内部标识必须原样保留。
2. 校验 tenant 实例运行配置：``DEPLOYMENT_MODE=tenant``、``TENANT_ID``/``INSTANCE_ID``
   与 Worker 固定绑定一致、租户在 allowed_tenants 白名单内。
3. 生成停写前后对账 SQL（只读）：行数、最大 ID、稳定表指纹、关联一致性检查。
4. 比较两份对账快照（TSV）：稳定表必须完全一致；追加型表只允许增长；关联检查必须为 0。
5. ``self-test`` 自带离线断言，不连接数据库/不读写生产。

设计约束
--------
* 本工具只生成/校验文件与 SQL，**不连接 MySQL、不执行迁移、不改生产数据**；
  实际执行由 ``deploy/scripts/tenant-onboarding/reconcile.sh``/``migrate.sh`` 在人工审批后完成。
* 退出码：0=通过；1=校验/对账失败；2=参数或文件错误。
* 表清单与字段注释以 ``app.db.registry`` 为准；无法导入时可用 ``--no-metadata`` 降级为结构校验。

常用命令
--------
```bash
python backend/scripts/tenant_migration_whitelist.py check \\
  --env-file /opt/ventasync/deploy/.env \\
  --whitelist deploy/scripts/tenant-onboarding/migration-whitelist.json

python backend/scripts/tenant_migration_whitelist.py emit-sql \\
  --tenant-id 3 --tenant-db vs_tenant_kangkang_shop --center-db ventasync_center > /tmp/reconcile-pre.sql

python backend/scripts/tenant_migration_whitelist.py compare-tsv \\
  --before /tmp/reconcile-pre.tsv --after /tmp/reconcile-post.tsv \\
  --whitelist deploy/scripts/tenant-onboarding/migration-whitelist.json

python backend/scripts/tenant_migration_whitelist.py self-test
```
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WHITELIST = (
    BACKEND_ROOT.parent / "deploy" / "scripts" / "tenant-onboarding" / "migration-whitelist.json"
)
NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")
Forbidden_COLUMN_PATTERNS = ("_cipher", "secret", "partner_key")
ALLOWED_SENSITIVE_COLUMNS = {"users.password_hash"}


class CheckError(RuntimeError):
    """校验失败（退出码 1）。"""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckError(f"读取白名单失败：{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CheckError("白名单根节点必须是 JSON 对象")
    return data


def _load_env(path: str | Path | None) -> dict[str, str]:
    if not path:
        return {}
    env_path = Path(path)
    if not env_path.is_file():
        raise CheckError(f"环境文件不存在：{env_path}")
    values: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _int(value: str, name: str, *, allow_zero: bool = False) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise CheckError(f"{name} 必须是整数，实际为 {value!r}") from exc
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise CheckError(f"{name} 必须为正整数，实际为 {parsed}")
    return parsed


def _metadata_tables(prefix: str) -> dict[str, Any]:
    """导入模型元数据（可选）；失败时返回空字典。"""
    try:
        if str(BACKEND_ROOT) not in sys.path:
            sys.path.insert(0, str(BACKEND_ROOT))
        from app.db.registry import center_metadata, tenant_metadata

        metadata = center_metadata if prefix == "center" else tenant_metadata
        return dict(metadata.tables)
    except Exception:  # noqa: BLE001 - 离线/无应用依赖时降级
        return {}


def validate_whitelist(whitelist: dict[str, Any], *, require_metadata: bool = True) -> list[str]:
    """返回问题列表；空列表表示通过。"""
    problems: list[str] = []
    if int(whitelist.get("version") or 0) < 1:
        problems.append("白名单缺少有效 version")
    preserve = [str(item) for item in whitelist.get("preserve_columns") or []]
    for required in ("id", "tenant_id", "member_no"):
        if required not in preserve:
            problems.append(f"preserve_columns 缺少内部标识：{required}")

    center_tables = whitelist.get("center_tables") or {}
    tenant_tables = whitelist.get("tenant_tables") or {}
    forbidden = whitelist.get("forbidden_center_tables") or {}
    if not isinstance(center_tables, dict) or not isinstance(tenant_tables, dict):
        return problems + ["center_tables/tenant_tables 必须是对象"]
    if set(center_tables) & set(forbidden):
        problems.append(f"center_tables 包含禁止表：{sorted(set(center_tables) & set(forbidden))}")

    center_meta = _metadata_tables("center") if require_metadata else {}
    tenant_meta = _metadata_tables("tenant") if require_metadata else {}
    if require_metadata and not center_meta:
        problems.append("无法导入 app.db.registry 模型元数据（可用 --no-metadata 降级）")

    for name, spec in center_tables.items():
        if not NAME_RE.match(name):
            problems.append(f"center_tables 表名非法：{name!r}")
        columns = [str(item) for item in (spec or {}).get("columns") or []]
        if not columns:
            problems.append(f"center_tables.{name} 缺少 columns 白名单")
        if "id" not in columns:
            problems.append(f"center_tables.{name} 未保留 id")
        for column in columns:
            lowered = column.lower()
            if column == "password_hash" and name != "users":
                problems.append(f"center_tables.{name}.{column} 不允许导出密码哈希")
                continue
            if f"{name}.{column}" in ALLOWED_SENSITIVE_COLUMNS:
                continue
            if any(pattern in lowered for pattern in Forbidden_COLUMN_PATTERNS):
                problems.append(f"center_tables.{name}.{column} 命中禁止列模式（密钥/令牌）")
        if name in center_meta:
            actual = {column.name for column in center_meta[name].columns}
            missing = [column for column in columns if column not in actual]
            if missing:
                problems.append(f"center_tables.{name} 列不存在：{missing}")
            if name == "tenant_memberships" and "member_no" not in columns:
                problems.append("tenant_memberships 必须保留 member_no")
        elif require_metadata:
            problems.append(f"center_tables 含未知表：{name}")

    skipped_tables: list[str] = []
    for name, spec in tenant_tables.items():
        if not NAME_RE.match(name):
            problems.append(f"tenant_tables 表名非法：{name!r}")
            continue
        spec = spec or {}
        if spec.get("migrate") is False:
            skipped_tables.append(name)
            continue
        if "id" not in preserve:
            problems.append(f"tenant_tables.{name} 需要保留 id")
        if name == "tenant_local_users" and spec.get("migrate") is not False:
            problems.append("tenant_local_users 已废弃，不应迁移（migrate=false）")
        if name in tenant_meta:
            actual = {column.name for column in tenant_meta[name].columns}
            if "tenant_id" not in actual:
                problems.append(f"tenant_tables.{name} 缺 tenant_id，疑似非租户表")
        elif require_metadata:
            problems.append(f"tenant_tables 含未知表：{name}")

    if require_metadata and tenant_meta:
        missing_tables = sorted(set(tenant_meta) - set(tenant_tables))
        if missing_tables:
            problems.append(f"tenant_tables 未覆盖模型表：{missing_tables}")
    if require_metadata and center_meta and not center_tables:
        problems.append("center_tables 不能为空")

    duplicates = [name for name in tenant_tables if list(tenant_tables).count(name) > 1]
    if duplicates:
        problems.append(f"tenant_tables 重复：{sorted(set(duplicates))}")
    if skipped_tables:
        # 跳过表必须有明确说明，避免静默丢数据
        for name in skipped_tables:
            note = str((tenant_tables.get(name) or {}).get("note") or "")
            if not note:
                problems.append(f"tenant_tables.{name} 标记不迁移但缺少 note 说明")
    return problems


def check_env(
    whitelist: dict[str, Any], env: dict[str, str], *, require_worker_binding: bool = True
) -> list[str]:
    """校验 tenant 实例运行环境与白名单；返回问题列表。"""
    problems: list[str] = []
    if not env:
        return ["未提供环境文件（--env-file），无法校验 tenant 实例绑定"]
    mode = env.get("DEPLOYMENT_MODE", "")
    if mode != "tenant":
        problems.append(f"DEPLOYMENT_MODE 必须为 tenant，实际为 {mode!r}")
    tenant_id = _int(env.get("TENANT_ID", ""), "TENANT_ID")
    instance_id = _int(env.get("INSTANCE_ID", ""), "INSTANCE_ID")

    allowed = whitelist.get("allowed_tenants") or []
    matched = next((item for item in allowed if int(item.get("tenant_id") or 0) == tenant_id), None)
    if not matched:
        problems.append(f"TENANT_ID={tenant_id} 不在 allowed_tenants 白名单内")
    else:
        expected_instance = int(matched.get("instance_id") or 0)
        if expected_instance and expected_instance != instance_id:
            problems.append(
                f"INSTANCE_ID={instance_id} 与白名单实例 ID={expected_instance} 不一致"
            )
        domain = str(matched.get("domain") or "").strip().lower()
        origin = env.get("APP_ORIGIN", "").strip().lower()
        if domain and origin:
            origin_host = re.sub(r"^https?://", "", origin).split("/")[0].split(":")[0]
            if origin_host != domain:
                problems.append(f"APP_ORIGIN={origin!r} 与白名单域名 {domain!r} 不一致")

    if require_worker_binding:
        worker_tenant = env.get("ERP_WORKER_TENANT_ID", "")
        worker_instance = env.get("ERP_WORKER_INSTANCE_ID", "")
        strict = env.get("ERP_WORKER_STRICT_BINDING", "true").lower()
        if str(worker_tenant) != str(tenant_id):
            problems.append(
                f"ERP_WORKER_TENANT_ID={worker_tenant!r} 必须等于 TENANT_ID={tenant_id}"
            )
        if str(worker_instance) != str(instance_id):
            problems.append(
                f"ERP_WORKER_INSTANCE_ID={worker_instance!r} 必须等于 INSTANCE_ID={instance_id}"
            )
        if strict not in {"1", "true", "yes"}:
            problems.append("ERP_WORKER_STRICT_BINDING 必须为 true（tenant 模式）")

    if env.get("BACKUP_INSTANCE_ID"):
        backup_instance = _int(env["BACKUP_INSTANCE_ID"], "BACKUP_INSTANCE_ID")
        if backup_instance != instance_id:
            problems.append(
                f"BACKUP_INSTANCE_ID={backup_instance} 必须等于 INSTANCE_ID={instance_id}"
            )
    if not env.get("APP_ORIGIN", "").startswith("https://"):
        problems.append("APP_ORIGIN 必须是 https 地址（生产 tenant 实例）")
    if env.get("SESSION_COOKIE_SECURE", "false").lower() != "true":
        problems.append("SESSION_COOKIE_SECURE 必须为 true（HTTPS 生产）")
    if env.get("CENTER_DATABASE_URL", "") and env.get("TENANT_DATABASE_URL", ""):
        if env["CENTER_DATABASE_URL"] == env["TENANT_DATABASE_URL"]:
            problems.append("CENTER_DATABASE_URL 与 TENANT_DATABASE_URL 不能相同")
    if env.get("COMPOSE_PROJECT_NAME", "").startswith("ventasync-drill"):
        problems.append("COMPOSE_PROJECT_NAME 不能使用演练项目名")
    return problems


def _db_identifier(value: str, name: str) -> str:
    if not NAME_RE.match(value or ""):
        raise CheckError(f"{name} 非法数据库名：{value!r}")
    return value


def _table_columns(prefix: str, table: str, fallback: list[str]) -> list[str]:
    meta = _metadata_tables(prefix)
    if table in meta:
        return [column.name for column in meta[table].columns]
    return fallback or ["id"]


def emit_sql(
    whitelist: dict[str, Any],
    *,
    tenant_id: int,
    tenant_db: str,
    center_db: str,
    phase: str = "pre",
) -> str:
    """生成停写前后对账 SQL（只读）。"""
    tenant_db = _db_identifier(tenant_db, "tenant_db")
    center_db = _db_identifier(center_db, "center_db")
    lines: list[str] = [
        f"-- 租户 {tenant_id} 迁移对账 SQL（phase={phase}）；只读，不修改数据",
        "-- 输出格式：label<TAB>rows<TAB>max_id<TAB>fingerprint",
    ]
    fp_columns = [str(item) for item in whitelist.get("preserve_columns") or []]

    for table, spec in (whitelist.get("center_tables") or {}).items():
        columns = [str(item) for item in (spec or {}).get("columns") or []]
        fp_expr = _fingerprint_expr(columns or fp_columns or ["id"])
        if table == "tenant_memberships":
            where = f" WHERE tenant_id={tenant_id}"
        elif table == "subscriptions":
            where = f" WHERE tenant_id={tenant_id}"
        elif table == "tenants":
            where = f" WHERE id={tenant_id}"
        elif table == "users":
            where = (
                f" WHERE id IN (SELECT user_id FROM `{center_db}`.`tenant_memberships` "
                f"WHERE tenant_id={tenant_id})"
            )
        else:
            where = ""
        lines.append(
            f"SELECT 'center.{table}' AS label, COUNT(*) AS rows, "
            f"COALESCE(MAX(id),0) AS max_id, {fp_expr} AS fingerprint "
            f"FROM `{center_db}`.`{table}`{where};"
        )

    for table, spec in (whitelist.get("tenant_tables") or {}).items():
        spec = spec or {}
        if spec.get("migrate") is False:
            continue
        columns = _table_columns("tenant", table, fp_columns)
        stable = bool(spec.get("stable"))
        fp_expr = _fingerprint_expr(columns if stable else [])
        if stable:
            lines.append(
                f"SELECT 'tenant.{table}' AS label, COUNT(*) AS rows, "
                f"COALESCE(MAX(id),0) AS max_id, {fp_expr} AS fingerprint "
                f"FROM `{tenant_db}`.`{table}` WHERE tenant_id={tenant_id};"
            )
        else:
            lines.append(
                f"SELECT 'tenant.{table}' AS label, COUNT(*) AS rows, "
                f"COALESCE(MAX(id),0) AS max_id, 0 AS fingerprint "
                f"FROM `{tenant_db}`.`{table}` WHERE tenant_id={tenant_id};"
            )

    for relation in whitelist.get("relation_checks") or []:
        name = str(relation.get("name") or "")
        sql = str(relation.get("sql") or "")
        if not name or not sql:
            continue
        sql = sql.format(center_db=center_db, tenant_db=tenant_db, tenant_id=tenant_id)
        lines.append(f"SELECT 'rel.{name}' AS label, ({sql}) AS rows, 0 AS max_id, 0 AS fingerprint;")
    return "\n".join(lines) + "\n"


def _fingerprint_expr(columns: list[str]) -> str:
    if not columns:
        return "0"
    parts = [f"COALESCE(CAST(`{column}` AS CHAR), '∅')" for column in columns]
    return f"COALESCE(SUM(CRC32(CONCAT_WS('|', {', '.join(parts)}))), 0)"


def _parse_tsv(path: Path) -> dict[str, tuple[int, int, int]]:
    rows: dict[str, tuple[int, int, int]] = {}
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckError(f"读取对账快照失败：{path}: {exc}") from exc
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("label\t") or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            raise CheckError(f"对账快照行格式非法：{line!r}")
        label, rows_raw, max_raw, fp_raw = parts[0], parts[1], parts[2], parts[3]
        try:
            rows[label] = (int(rows_raw), int(max_raw), int(fp_raw))
        except ValueError as exc:
            raise CheckError(f"对账快照数值非法：{line!r}") from exc
    if not rows:
        raise CheckError(f"对账快照为空：{path}")
    return rows


def compare_tsv(
    before: dict[str, tuple[int, int, int]],
    after: dict[str, tuple[int, int, int]],
    whitelist: dict[str, Any],
    *,
    allow_growth: bool = True,
) -> list[str]:
    """比较两份快照；返回问题列表。"""
    problems: list[str] = []
    missing_after = sorted(set(before) - set(after))
    missing_before = sorted(set(after) - set(before))
    if missing_after:
        problems.append(f"迁移后快照缺少对象：{missing_after}")
    if missing_before:
        problems.append(f"迁移前快照缺少对象：{missing_before}")
    stable_tables = {
        f"center.{name}" for name in (whitelist.get("center_tables") or {})
    } | {
        f"tenant.{name}"
        for name, spec in (whitelist.get("tenant_tables") or {}).items()
        if (spec or {}).get("stable")
    }
    for label in sorted(set(before) & set(after)):
        before_rows, before_max, before_fp = before[label]
        after_rows, after_max, after_fp = after[label]
        if label.startswith("rel."):
            if after_rows != 0:
                problems.append(f"关联检查失败 {label}：迁移后计数={after_rows}（应为 0）")
            if before_rows != 0:
                problems.append(f"关联检查基线异常 {label}：迁移前计数={before_rows}（应为 0）")
            continue
        if label in stable_tables:
            if (before_rows, before_max, before_fp) != (after_rows, after_max, after_fp):
                problems.append(
                    f"稳定表不一致 {label}：before={before} after={after}"
                )
            continue
        if allow_growth:
            if after_rows < before_rows or after_max < before_max:
                problems.append(
                    f"追加型表出现回退 {label}：before rows={before_rows} max={before_max} "
                    f"after rows={after_rows} max={after_max}"
                )
        elif (before_rows, before_max) != (after_rows, after_max):
            problems.append(f"表计数变化 {label}：before rows={before_rows} after={after_rows}")
    return problems


def self_test() -> int:
    """离线自测：校验白名单/环境/对账比较逻辑。"""
    whitelist = _read_json(DEFAULT_WHITELIST)
    problems = validate_whitelist(whitelist, require_metadata=False)
    if problems:
        print("SELF-TEST FAIL: 默认白名单结构校验失败", file=sys.stderr)
        return 1
    good_env = {
        "DEPLOYMENT_MODE": "tenant",
        "TENANT_ID": "3",
        "INSTANCE_ID": "2",
        "APP_ORIGIN": "https://zuhu.alienyan.store",
        "SESSION_COOKIE_SECURE": "true",
        "ERP_WORKER_TENANT_ID": "3",
        "ERP_WORKER_INSTANCE_ID": "2",
        "ERP_WORKER_STRICT_BINDING": "true",
        "BACKUP_INSTANCE_ID": "2",
        "CENTER_DATABASE_URL": "mysql+pymysql://u:p@center/ventasync_center",
        "TENANT_DATABASE_URL": "mysql+pymysql://u:p@tenant/vs_tenant_3",
        "COMPOSE_PROJECT_NAME": "ventasync-tenant-3",
    }
    if check_env(whitelist, good_env):
        print("SELF-TEST FAIL: 合法环境不应报错", file=sys.stderr)
        return 1
    bad_env = dict(good_env, ERP_WORKER_TENANT_ID="1")
    if not check_env(whitelist, bad_env):
        print("SELF-TEST FAIL: Worker 绑定不一致应失败", file=sys.stderr)
        return 1
    center_spec = whitelist["center_tables"]
    center_spec["tenants"]["columns"].append("password_cipher")
    if not validate_whitelist(whitelist, require_metadata=False):
        print("SELF-TEST FAIL: 密钥列应被拒绝", file=sys.stderr)
        return 1
    center_spec["tenants"]["columns"].pop()
    before = {"tenant.roles": (3, 9, 100), "tenant.orders": (10, 50, 0), "rel.member_roles_without_role": (0, 0, 0)}
    after = {"tenant.roles": (3, 9, 100), "tenant.orders": (12, 55, 0), "rel.member_roles_without_role": (0, 0, 0)}
    if compare_tsv(before, after, whitelist):
        print("SELF-TEST FAIL: 合法增长不应报错", file=sys.stderr)
        return 1
    after["tenant.roles"] = (2, 9, 99)
    if not compare_tsv(before, after, whitelist):
        print("SELF-TEST FAIL: 稳定表变化应失败", file=sys.stderr)
        return 1
    print("SELF-TEST OK: 白名单结构/env 绑定/对账比较/禁止列检查通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第二租户迁移白名单与停写对账工具")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="校验白名单与 tenant 实例环境")
    check.add_argument("--whitelist", default=str(DEFAULT_WHITELIST))
    check.add_argument("--env-file", default="")
    check.add_argument("--no-metadata", action="store_true", help="跳过模型元数据交叉校验")

    emit = sub.add_parser("emit-sql", help="生成只读对账 SQL")
    emit.add_argument("--whitelist", default=str(DEFAULT_WHITELIST))
    emit.add_argument("--env-file", default="")
    emit.add_argument("--tenant-id", type=int, default=0)
    emit.add_argument("--tenant-db", required=True)
    emit.add_argument("--center-db", required=True)
    emit.add_argument("--phase", choices=["pre", "post"], default="pre")

    compare = sub.add_parser("compare-tsv", help="比较停写前后对账快照")
    compare.add_argument("--whitelist", default=str(DEFAULT_WHITELIST))
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    compare.add_argument("--no-allow-growth", action="store_true")

    sub.add_parser("self-test", help="离线自测")

    args = parser.parse_args(argv)
    try:
        if args.command == "self-test":
            return self_test()
        whitelist = _read_json(Path(args.whitelist))
        if args.command == "check":
            problems = validate_whitelist(whitelist, require_metadata=not args.no_metadata)
            env = _load_env(args.env_file) if args.env_file else {}
            problems.extend(check_env(whitelist, env) if args.env_file else [])
            if problems:
                for problem in problems:
                    print(f"[FAIL] {problem}", file=sys.stderr)
                return 1
            print(json.dumps({"ok": True, "tables": len(whitelist.get("tenant_tables") or {})}, ensure_ascii=False))
            return 0
        if args.command == "emit-sql":
            env = _load_env(args.env_file) if args.env_file else {}
            tenant_id = args.tenant_id or int(env.get("TENANT_ID") or 0)
            if tenant_id <= 0:
                raise CheckError("emit-sql 需要 --tenant-id 或环境文件中的 TENANT_ID")
            print(
                emit_sql(
                    whitelist,
                    tenant_id=tenant_id,
                    tenant_db=args.tenant_db,
                    center_db=args.center_db,
                    phase=args.phase,
                ),
                end="",
            )
            return 0
        if args.command == "compare-tsv":
            before = _parse_tsv(Path(args.before))
            after = _parse_tsv(Path(args.after))
            problems = compare_tsv(
                before, after, whitelist, allow_growth=not args.no_allow_growth
            )
            if problems:
                for problem in problems:
                    print(f"[FAIL] {problem}", file=sys.stderr)
                return 1
            print(f"OK: {len(before)} 个对象全部一致（追加型表仅允许增长）")
            return 0
    except CheckError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
