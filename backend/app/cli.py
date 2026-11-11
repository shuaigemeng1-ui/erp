"""运维命令行入口。

用法（在 backend/ 目录）：

```bash
python -m app.cli platform-init                 # 创建首个平台管理员（不开放公共注册）
python -m app.cli platform-init --email a@b.c --password '...' --name 管理员
python -m app.cli tenant-migrate --db-url mysql+pymysql://user:pass@host:3306/vs_tenant_1
python -m app.cli config-check                  # 打印脱敏配置并检查依赖版本
```

约定：

* 初始化命令可重复执行；已存在同邮箱平台管理员时直接提示，不重复创建。
* 迁移使用独立发布任务，绝不在 API 启动时自动执行。
* 命令不启动 HTTP 服务，不执行任何租户业务操作。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import subprocess
import sys
import tomllib
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _mask_url(url: str) -> str:
    """脱敏数据库 URL 中的密码。"""
    if not url:
        return ""
    if "@" not in url:
        return url
    head, _, tail = url.rpartition("@")
    if "://" not in head:
        return url
    scheme, _, credentials = head.partition("://")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{tail}"


def _platform_init(args: argparse.Namespace) -> int:
    from app.core.config import settings

    email = args.email or settings.bootstrap_platform_admin_email
    password = args.password or settings.bootstrap_platform_admin_password
    display_name = args.name or settings.bootstrap_platform_admin_name
    if not email or not password:
        print(
            "缺少参数：请提供 --email/--password 或配置 BOOTSTRAP_PLATFORM_ADMIN_EMAIL/"
            "BOOTSTRAP_PLATFORM_ADMIN_PASSWORD",
            file=sys.stderr,
        )
        return 2

    from app.db.session import center_session

    try:
        from app.modules.identity.provisioning import bootstrap_platform_admin
    except (ImportError, AttributeError):
        print("identity.provisioning.bootstrap_platform_admin 尚未实现", file=sys.stderr)
        return 3

    with center_session() as center:
        try:
            result = bootstrap_platform_admin(
                center,
                email=email,
                password=password,
                display_name=display_name,
            )
            center.commit()
        except Exception:
            center.rollback()
            raise
    user_id = getattr(result, "id", result)
    print(f"平台管理员已就绪：id={user_id} email={email} display_name={display_name}")
    return 0


def _tenant_migrate(args: argparse.Namespace) -> int:
    if not args.db_url:
        print("缺少 --db-url（格式 mysql+pymysql://用户:密码@主机:端口/库名）", file=sys.stderr)
        return 2
    command = [
        sys.executable,
        "-m",
        "alembic",
        "-c",
        "alembic/tenant.ini",
        "-x",
        f"db_url={args.db_url}",
        "upgrade",
        "head",
    ]
    print("执行租户库迁移:", " ".join(command))
    completed = subprocess.run(command, cwd=BACKEND_DIR, check=False)
    return completed.returncode


def _config_check(args: argparse.Namespace) -> int:
    from app.core.config import settings
    from app.core.permissions import PERMISSION_CODES

    print(f"app_name             = {settings.app_name}")
    print(f"app_env              = {settings.app_env}")
    print(f"center_database_url  = {_mask_url(settings.center_database_url)}")
    print(f"provisioner_url      = {_mask_url(settings.provisioner_database_url)}")
    print(f"cache_redis_url      = {settings.cache_redis_url}")
    print(f"queue_redis_url      = {settings.queue_redis_url}")
    print(f"session_cookie_secure= {settings.session_cookie_secure}")
    print(f"permissions          = {len(PERMISSION_CODES)} 个")
    print("依赖版本:")
    for package in (
        "fastapi",
        "pydantic",
        "SQLAlchemy",
        "alembic",
        "PyMySQL",
        "redis",
        "celery",
        "httpx",
        "cryptography",
        "bcrypt",
    ):
        try:
            print(f"  {package:<14} {importlib.metadata.version(package)}")
        except importlib.metadata.PackageNotFoundError:
            print(f"  {package:<14} 未安装")
    pyproject = BACKEND_DIR / "pyproject.toml"
    if pyproject.exists():
        with pyproject.open("rb") as handle:
            print("pyproject:", tomllib.load(handle).get("project", {}).get("name", "(未命名)"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="VentaSync 运维命令",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    platform_init = subparsers.add_parser("platform-init", help="创建首个平台管理员")
    platform_init.add_argument("--email", default="")
    platform_init.add_argument("--password", default="")
    platform_init.add_argument("--name", default="")
    platform_init.set_defaults(handler=_platform_init)

    tenant_migrate = subparsers.add_parser("tenant-migrate", help="对单个租户库执行迁移")
    tenant_migrate.add_argument("--db-url", required=True)
    tenant_migrate.set_defaults(handler=_tenant_migrate)

    config_check = subparsers.add_parser("config-check", help="打印脱敏配置与依赖版本")
    config_check.set_defaults(handler=_config_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认编码可能不是 UTF-8，统一输出编码，避免中文乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
