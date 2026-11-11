"""租户库 Alembic 环境。

每个租户库使用独立的版本表（默认 ``alembic_version``），由开通任务或发布任务执行。
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db.registry import tenant_metadata

config = context.config
target_metadata = tenant_metadata


def _database_url() -> str:
    x_arguments = context.get_x_argument(as_dictionary=True)
    url = x_arguments.get("db_url") or os.environ.get("TENANT_DATABASE_URL") or ""
    if not url:
        raise RuntimeError(
            "未指定租户库连接：请使用 -x db_url=mysql+pymysql://用户:密码@主机:端口/库名 或设置 TENANT_DATABASE_URL"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {}) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
