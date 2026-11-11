#!/usr/bin/env python
"""发布迁移入口：中心库 + 全部租户库执行 Alembic ``upgrade head``。

设计约束（plan.txt §1 / §5）：
* 迁移只由独立发布任务执行，API / Worker / 调度器启动时不迁移；
* 中心库与租户库使用两套独立迁移（``alembic/center.ini`` / ``alembic/tenant.ini``）；
* 建库、建号、迁移使用独立运维身份（``PROVISIONER_DATABASE_URL``），
  应用运行账号（``CENTER_DATABASE_URL``）只访问中心库；
* 同一时刻只允许一个发布任务执行（MySQL 命名锁），失败立即退出码 1。

用法（在 backend/ 目录或容器 /app 内）::

    python scripts/migrate_all.py                  # 中心库 + 全部租户库
    python scripts/migrate_all.py --target center  # 只升级中心库
    python scripts/migrate_all.py --target tenant  # 只升级租户库
    python scripts/migrate_all.py --tenant-id 3    # 只升级指定租户
    python scripts/migrate_all.py --database vs_tenant_3
    python scripts/migrate_all.py --list           # 只列出将处理的目标（仍会连中心库）

Compose 中的用法::

    docker compose --profile release run --rm migrate
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

# 允许以 ``python scripts/migrate_all.py`` 方式运行（sys.path[0] 是 scripts/）
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings  # noqa: E402  （必须先修正 sys.path）

logger = logging.getLogger("ventasync.migrate")

CENTER_ALEMBIC_INI = BACKEND_ROOT / "alembic" / "center.ini"
TENANT_ALEMBIC_INI = BACKEND_ROOT / "alembic" / "tenant.ini"
MIGRATE_LOCK_NAME = "ventasync:migrate"
MIGRATE_LOCK_TIMEOUT = 120

TenantTarget = tuple[int, str, str]  # (tenant_id, database_name, sqlalchemy_url)


def _mask(url: str) -> str:
    """隐藏连接串中的密码，避免日志泄漏。"""
    try:
        parsed = make_url(url)
        if parsed.password:
            parsed = parsed.set(password="***")
        return parsed.render_as_string(hide_password=False)
    except Exception:  # noqa: BLE001 - 非法 URL 时原样返回（仅用于日志）
        return url


def _provisioner_base_url() -> URL:
    """运维身份连接串（建库 / 迁移专用）。未配置时回退到中心库账号。"""
    raw = settings.provisioner_database_url or settings.center_database_url
    return make_url(raw)


def _tenant_url(database_name: str, host: str | None, port: int | None) -> str:
    """用运维身份拼出租户库连接串：库名来自中心库受控映射。"""
    base = _provisioner_base_url()
    url = base.set(
        drivername="mysql+pymysql",
        username=base.username,
        password=base.password,
        host=host or base.host or settings.tenant_db_host,
        port=port or base.port or settings.tenant_db_port,
        database=database_name,
        query={"charset": "utf8mb4"},
    )
    return url.render_as_string(hide_password=False)


def _list_tenant_databases(
    *, tenant_ids: set[int] | None, database: str | None
) -> list[TenantTarget]:
    """从中心库 ``tenant_databases`` 受控映射读取需要迁移的租户库。"""
    engine = create_engine(settings.center_database_url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT tenant_id, database_name, host, port "
                    "FROM tenant_databases ORDER BY tenant_id"
                )
            ).all()
    except SQLAlchemyError as exc:
        raise RuntimeError(
            "读取中心库 tenant_databases 失败：请先执行 `--target center` 升级中心库，"
            "并确认 CENTER_DATABASE_URL 指向已初始化的中心库。"
        ) from exc
    finally:
        engine.dispose()

    targets: list[TenantTarget] = []
    for tenant_id, database_name, host, port in rows:
        if not database_name:
            continue
        if tenant_ids and int(tenant_id) not in tenant_ids:
            continue
        if database and str(database_name) != database:
            continue
        targets.append(
            (
                int(tenant_id),
                str(database_name),
                _tenant_url(str(database_name), str(host) if host else None, int(port) if port else None),
            )
        )
    return targets


def _alembic_version(url: str) -> str:
    """读取目标库当前版本号，用于发布日志与回滚核对。"""
    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            value = connection.execute(
                text("SELECT version_num FROM alembic_version LIMIT 1")
            ).scalar()
            return str(value) if value else "(空)"
    except SQLAlchemyError:
        return "(尚未迁移)"
    finally:
        engine.dispose()


def _upgrade_center() -> None:
    logger.info("→ 升级中心库：%s", _mask(settings.center_database_url))
    config = Config(str(CENTER_ALEMBIC_INI))
    # env.py 优先读取 CENTER_DATABASE_URL，显式设置以覆盖残留环境变量
    os.environ["CENTER_DATABASE_URL"] = settings.center_database_url
    command.upgrade(config, "head")
    logger.info("  中心库当前版本：%s", _alembic_version(settings.center_database_url))


def _upgrade_tenant(target: TenantTarget) -> None:
    tenant_id, database_name, url = target
    logger.info("→ 升级租户库 tenant_id=%s db=%s：%s", tenant_id, database_name, _mask(url))
    config = Config(str(TENANT_ALEMBIC_INI))
    # tenant/env.py 优先读取 -x db_url，其次 TENANT_DATABASE_URL；进程内用环境变量传递
    os.environ["TENANT_DATABASE_URL"] = url
    try:
        command.upgrade(config, "head")
    finally:
        os.environ.pop("TENANT_DATABASE_URL", None)
    logger.info("  租户库当前版本：%s", _alembic_version(url))


def _acquire_lock(connection) -> None:
    """MySQL 命名锁：防止两个发布任务并发执行迁移。"""
    got = connection.execute(
        text("SELECT GET_LOCK(:name, :timeout)"),
        {"name": MIGRATE_LOCK_NAME, "timeout": MIGRATE_LOCK_TIMEOUT},
    ).scalar()
    if got != 1:
        raise RuntimeError(
            "另一个发布任务正在执行迁移（未能获取 MySQL 命名锁），请稍后重试。"
        )


def _release_lock(connection) -> None:
    try:
        connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": MIGRATE_LOCK_NAME})
    except SQLAlchemyError:  # pragma: no cover - 释放失败不阻塞发布结果
        logger.warning("释放迁移锁失败，连接关闭后会自动释放")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="VentaSync 发布迁移：中心库与租户库执行 alembic upgrade head",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target",
        choices=["all", "center", "tenant"],
        default="all",
        help="迁移目标（默认 all：中心库 + 全部租户库）",
    )
    parser.add_argument(
        "--tenant-id",
        action="append",
        type=int,
        help="只迁移指定租户 ID，可重复传入",
    )
    parser.add_argument(
        "--database",
        help="只迁移指定租户库名（与 --tenant-id 二选一）",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="只列出迁移目标，不执行迁移",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    os.chdir(BACKEND_ROOT)  # env.py 的 prepend_sys_path / script_location 依赖相对路径

    if args.target == "center" and (args.tenant_id or args.database):
        parser.error("--target center 不能与 --tenant-id / --database 同时使用")

    started = time.time()
    tenant_targets: list[TenantTarget] = []

    lock_engine = create_engine(settings.center_database_url, pool_pre_ping=True, future=True)
    try:
        with lock_engine.connect() as lock_connection:
            if not args.list:
                _acquire_lock(lock_connection)
            try:
                if args.target in ("all", "center"):
                    if args.list:
                        logger.info("[dry-run] 将升级中心库：%s", _mask(settings.center_database_url))
                    else:
                        _upgrade_center()

                if args.target in ("all", "tenant"):
                    tenant_ids = set(args.tenant_id) if args.tenant_id else None
                    tenant_targets = _list_tenant_databases(
                        tenant_ids=tenant_ids, database=args.database
                    )
                    if not tenant_targets:
                        logger.warning("中心库中没有需要迁移的租户库（tenant_databases 为空或均已过滤）")
                    for target in tenant_targets:
                        if args.list:
                            logger.info(
                                "[dry-run] 将升级租户库 tenant_id=%s db=%s",
                                target[0],
                                target[1],
                            )
                        else:
                            _upgrade_tenant(target)
            finally:
                if not args.list:
                    _release_lock(lock_connection)
    except Exception as exc:  # noqa: BLE001 - 发布任务必须明确失败
        logger.error("迁移失败：%s", exc)
        return 1
    finally:
        lock_engine.dispose()

    elapsed = time.time() - started
    logger.info(
        "迁移完成：中心库=%s，租户库=%d 个，耗时 %.1fs",
        "已执行" if args.target in ("all", "center") else "跳过",
        len(tenant_targets),
        elapsed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
