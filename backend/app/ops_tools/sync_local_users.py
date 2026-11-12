"""把中心租户成员同步为租户本机账号（阶段二基础）。

在管理中心容器内运行：
  python -m app.ops_tools.sync_local_users --tenant-id 3

只同步已设置密码的用户；不覆盖本机已修改的密码（除非 --force-password）。
"""

from __future__ import annotations

import argparse
from datetime import datetime

from sqlalchemy import select

from app.db.base import utcnow
from app.db.session import center_session, tenant_session
from app.modules.identity.models_center import TenantMembership, User
from app.modules.identity.models_tenant import TenantLocalUser
from app.modules.tasks.tenancy import load_tenant_route


def sync(tenant_id: int, *, force_password: bool = False) -> dict[str, int]:
    with center_session() as center:
        route = load_tenant_route(center, tenant_id)
        rows = center.execute(
            select(TenantMembership, User)
            .join(User, User.id == TenantMembership.user_id)
            .where(TenantMembership.tenant_id == tenant_id)
        ).all()
        with tenant_session(route) as db:
            created = updated = skipped = 0
            now = utcnow()
            for membership, user in rows:
                if not user.password_hash:
                    skipped += 1
                    continue
                row = db.execute(
                    select(TenantLocalUser).where(
                        TenantLocalUser.tenant_id == tenant_id,
                        TenantLocalUser.source_user_id == int(user.id),
                    )
                ).scalar_one_or_none()
                if row is None:
                    db.add(
                        TenantLocalUser(
                            tenant_id=tenant_id,
                            source_user_id=int(user.id),
                            email=user.email.lower(),
                            password_hash=user.password_hash if force_password else user.password_hash,
                            display_name=user.display_name or "",
                            member_no=int(membership.member_no) if membership.member_no else None,
                            is_owner=bool(membership.is_owner),
                            status="active" if str(user.status) == "active" else "disabled",
                            synced_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    created += 1
                else:
                    row.email = user.email.lower()
                    row.display_name = user.display_name or ""
                    row.member_no = int(membership.member_no) if membership.member_no else None
                    row.is_owner = bool(membership.is_owner)
                    row.status = "active" if str(user.status) == "active" else "disabled"
                    row.synced_at = now
                    row.updated_at = now
                    if force_password:
                        row.password_hash = user.password_hash
                    updated += 1
            db.commit()
    return {"created": created, "updated": updated, "skipped": skipped, "total": len(rows)}


def main() -> int:
    parser = argparse.ArgumentParser(description="同步中心成员到租户本机账号表")
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--force-password", action="store_true")
    args = parser.parse_args()
    print(sync(args.tenant_id, force_password=args.force_password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
