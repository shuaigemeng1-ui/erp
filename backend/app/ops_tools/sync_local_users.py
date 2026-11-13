"""【已废弃 / 非认证来源】把中心租户成员同步为租户本机账号（阶段二历史遗留）。

⚠️ 认证口径（批次2 起）：**中心库账号与成员关系是唯一认证来源**。
``tenant_local_users`` 表没有任何实际认证链路读取，只是早期「租户独立认证」的
历史数据副本；继续同步会产生第二套重复账号，属于安全风险，因此本工具默认禁用。

保留历史数据，不删除表。仅在明确知道影响时手工执行：

    python -m app.ops_tools.sync_local_users --tenant-id 3 --allow-deprecated

不要在任何自动任务/部署脚本中调用本工具；租户模式（deployment_mode=tenant）
的身份仍通过中心库账号与会话校验，绑定租户由 TENANT_ID 固定。
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.db.base import utcnow
from app.db.session import center_session, tenant_session
from app.modules.identity.models_center import TenantMembership, User
from app.modules.identity.models_tenant import TenantLocalUser
from app.modules.tasks.tenancy import load_tenant_route

DEPRECATED_MESSAGE = (
    "已废弃：tenant_local_users 不是认证来源（中心库账号/成员关系才是唯一认证来源）。"
    "如确需执行历史同步，请显式加 --allow-deprecated；不要用于部署或自动任务。"
)


def sync(tenant_id: int, *, force_password: bool = False, allow_deprecated: bool = False) -> dict[str, int]:
    """同步中心成员到租户本机账号表（历史遗留，默认禁用）。

    :raises RuntimeError: 未显式 ``allow_deprecated=True`` 时拒绝执行。
    """
    if not allow_deprecated:
        raise RuntimeError(DEPRECATED_MESSAGE)
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
    parser = argparse.ArgumentParser(
        description="【已废弃】同步中心成员到租户本机账号表（非认证来源，默认禁用）"
    )
    parser.add_argument("--tenant-id", type=int, default=0)
    parser.add_argument("--force-password", action="store_true")
    parser.add_argument(
        "--allow-deprecated",
        action="store_true",
        help="确认这是历史遗留操作、不会被任何认证链路使用后才允许执行",
    )
    args = parser.parse_args()
    if not args.allow_deprecated:
        print(DEPRECATED_MESSAGE, file=sys.stderr)
        return 2
    if not args.tenant_id:
        print("缺少 --tenant-id", file=sys.stderr)
        return 2
    print(sync(args.tenant_id, force_password=args.force_password, allow_deprecated=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
