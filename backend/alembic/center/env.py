"""中心库 Alembic 环境。"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.db.registry import center_metadata

config = context.config
target_metadata = center_metadata


def _database_url() -> str:
    """优先级：环境变量 CENTER_DATABASE_URL > ini 中的 sqlalchemy.url > 应用配置。"""
    return (
        os.environ.get("CENTER_DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
        or settings.center_database_url
    )


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
