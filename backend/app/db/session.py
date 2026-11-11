"""数据库连接管理。

* 中心库与租户库走不同的 Session 入口（``center_session`` / ``tenant_session``）。
* 租户库 Engine 按「租户 + 连接版本」缓存，设置池容量与闲置回收，控制总连接数。
* 业务数据库名、服务器与凭据只来自中心库受控映射（``TenantDbRoute``），
  不在共享连接上执行 ``USE`` 切换租户。
* 建库、建号、迁移使用独立运维身份（``provisioner_engine``）。

并发约定：所有惰性初始化使用可重入锁，并且在锁外调用其它惰性初始化函数，
避免「持锁后再取同一把锁」造成自死锁（历史缺陷，勿回退）。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.context import TenantDbRoute

_center_engine: Engine | None = None
_center_factory: sessionmaker[Session] | None = None
_provisioner_engine: Engine | None = None
_lock = threading.RLock()


def _engine_options(pool_size: int, max_overflow: int) -> dict[str, Any]:
    return {
        "pool_pre_ping": True,
        "pool_recycle": settings.tenant_pool_recycle_seconds,
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_timeout": 30,
        "future": True,
        "echo": False,
    }


def center_engine() -> Engine:
    """中心库 Engine（惰性创建，不建立连接）。"""
    global _center_engine
    engine = _center_engine
    if engine is None:
        with _lock:
            if _center_engine is None:
                _center_engine = create_engine(
                    settings.center_database_url,
                    **_engine_options(10, 10),
                )
            engine = _center_engine
    return engine


def center_session_factory() -> sessionmaker[Session]:
    """中心库 Session 工厂。

    先获取 Engine（可能自己加锁），再在锁内只创建 sessionmaker，
    绝不在持有 ``_lock`` 时调用 ``center_engine()``。
    """
    global _center_factory
    factory = _center_factory
    if factory is None:
        engine = center_engine()
        with _lock:
            if _center_factory is None:
                _center_factory = sessionmaker(
                    bind=engine,
                    autoflush=False,
                    expire_on_commit=False,
                    future=True,
                )
            factory = _center_factory
    return factory


@contextmanager
def center_session() -> Iterator[Session]:
    """中心库会话：请求/任务内独立创建、关闭，事务由调用方（Service）管理。"""
    session = center_session_factory()()
    try:
        yield session
    finally:
        session.close()


def provisioner_engine() -> Engine:
    """运维身份 Engine：仅用于建库、建号、迁移，不允许在业务请求中使用。"""
    global _provisioner_engine
    engine = _provisioner_engine
    if engine is None:
        with _lock:
            if _provisioner_engine is None:
                url = settings.provisioner_database_url or settings.center_database_url
                _provisioner_engine = create_engine(
                    url,
                    pool_pre_ping=True,
                    pool_size=2,
                    max_overflow=1,
                    future=True,
                    isolation_level="AUTOCOMMIT",
                )
            engine = _provisioner_engine
    return engine


class TenantEngineCache:
    """按「租户 + 连接版本」缓存 Engine，LRU 淘汰时释放连接池。"""

    def __init__(self, max_size: int | None = None) -> None:
        self._max_size = max_size or settings.tenant_engine_cache_size
        self._engines: OrderedDict[tuple[int, str, int, str, int], Engine] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, route: TenantDbRoute) -> Engine:
        key = route.cache_key()
        with self._lock:
            engine = self._engines.get(key)
            if engine is not None:
                self._engines.move_to_end(key)
                return engine
            engine = create_engine(
                route.sqlalchemy_url(),
                **_engine_options(settings.tenant_pool_size, settings.tenant_max_overflow),
            )
            self._engines[key] = engine
            while len(self._engines) > self._max_size:
                _, evicted = self._engines.popitem(last=False)
                evicted.dispose()
            return engine

    def invalidate_tenant(self, tenant_id: int) -> None:
        """租户库凭据变更后调用，丢弃旧 Engine 连接池。"""
        with self._lock:
            keys = [key for key in self._engines if key[0] == tenant_id]
            for key in keys:
                self._engines.pop(key).dispose()

    def dispose_all(self) -> None:
        with self._lock:
            for engine in self._engines.values():
                engine.dispose()
            self._engines.clear()


tenant_engines = TenantEngineCache()


@contextmanager
def tenant_session(route: TenantDbRoute) -> Iterator[Session]:
    """租户库会话。必须由已校验的租户上下文提供路由。"""
    session = sessionmaker(
        bind=tenant_engines.get(route),
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )()
    try:
        yield session
    finally:
        session.close()


def ping_center() -> bool:
    with center_engine().connect() as connection:
        connection.execute(text("SELECT 1"))
    return True


def current_database_name(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(text("SELECT DATABASE()")).scalar_one())


def dispose_all() -> None:
    """进程退出时释放连接池。"""
    global _center_engine, _center_factory, _provisioner_engine
    tenant_engines.dispose_all()
    with _lock:
        if _center_engine is not None:
            _center_engine.dispose()
            _center_engine = None
            _center_factory = None
        if _provisioner_engine is not None:
            _provisioner_engine.dispose()
            _provisioner_engine = None
