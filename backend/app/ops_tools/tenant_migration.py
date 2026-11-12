"""租户迁移导出工具（阶段五试点）。

在管理中心容器内运行：
  python -m app.ops_tools.tenant_migration --tenant-id 3 --output /tmp/tenant-migration/3

导出：
* 中心库：租户、成员关系、用户、套餐/订阅、渠道应用引用（不导出密钥）
* 租户库：全部业务表

生成 manifest.json：表名、行数、sha256、导出时间、源库标识。
不执行域名切换，不修改生产数据；用于迁移前核对与试点演练。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, select, text

from app.db.base import utcnow
from app.db.session import center_session, tenant_session
from app.modules.identity.models_center import (
    Plan,
    Subscription,
    Tenant,
    TenantMembership,
    User,
)
from app.modules.tasks.tenancy import load_tenant_route


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _dump_rows(conn, table: str) -> list[dict[str, Any]]:
    rows = conn.execute(text(f"SELECT * FROM `{table}`")).mappings().all()
    return [{key: _json_value(value) for key, value in dict(row).items()} for row in rows]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            handle.write(line + "\n")
            digest.update(line.encode("utf-8"))
    return digest.hexdigest()


def export_tenant(tenant_id: int, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "tenant_id": tenant_id,
        "exported_at": utcnow().isoformat() + "Z",
        "center": {},
        "tenant": {},
        "warnings": [
            "渠道应用密钥不随本包导出；由独立集成网关迁移",
            "本工具不切换域名、不停写、不修改生产数据",
        ],
    }
    with center_session() as center:
        tenant = center.get(Tenant, tenant_id)
        if tenant is None:
            raise SystemExit(f"租户不存在：{tenant_id}")
        route = load_tenant_route(center, tenant_id)
        memberships = center.execute(
            select(TenantMembership).where(TenantMembership.tenant_id == tenant_id)
        ).scalars().all()
        user_ids = [int(item.user_id) for item in memberships]
        users = center.execute(select(User).where(User.id.in_(user_ids))).scalars().all() if user_ids else []
        plans = center.execute(select(Plan)).scalars().all()
        subscriptions = center.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_id)
        ).scalars().all()

        center_dir = output / "center"
        center_dir.mkdir(exist_ok=True)
        center_tables = {
            "tenants": [tenant],
            "users": list(users),
            "tenant_memberships": list(memberships),
            "plans": list(plans),
            "subscriptions": list(subscriptions),
        }
        for name, rows in center_tables.items():
            payload = []
            for row in rows:
                payload.append({column.key: _json_value(getattr(row, column.key)) for column in row.__table__.columns})
            digest = _write_jsonl(center_dir / f"{name}.jsonl", payload)
            manifest["center"][name] = {"rows": len(payload), "sha256": digest}

        tenant_dir = output / "tenant"
        tenant_dir.mkdir(exist_ok=True)
        with tenant_session(route) as db:
            inspector = inspect(db.get_bind())
            for table in sorted(inspector.get_table_names()):
                rows = _dump_rows(db, table)
                digest = _write_jsonl(tenant_dir / f"{table}.jsonl", rows)
                manifest["tenant"][table] = {"rows": len(rows), "sha256": digest}

    manifest["totals"] = {
        "center_rows": sum(item["rows"] for item in manifest["center"].values()),
        "tenant_rows": sum(item["rows"] for item in manifest["tenant"].values()),
        "tenant_tables": len(manifest["tenant"]),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="导出租户迁移包并生成校验清单")
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    output = Path(args.output or f"/tmp/tenant-migration/{args.tenant_id}")
    manifest = export_tenant(args.tenant_id, output)
    print(json.dumps(manifest["totals"], ensure_ascii=False))
    print(f"manifest={output / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
