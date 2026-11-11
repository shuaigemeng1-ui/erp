"""SQLAlchemy 基础约定。

硬性规范（见 plan.txt §2「无外键规范」）：

* 模型禁止声明 ``ForeignKey`` / ``ForeignKeyConstraint``，关联只存普通 ID 列。
* 每张表必须包含 ``__table_args__`` 的中文表注释（``comment``），每个字段必须有中文注释。
* 业务唯一性用唯一约束表达，查询索引按需建立。
* 时间统一存 UTC（naive ``DATETIME``），按租户时区展示。
* 金额/汇率使用 ``DECIMAL``，Python 侧使用 ``Decimal``。
* 主键统一为 ``BIGINT`` 自增；JSON 输出时转为字符串，避免前端精度丢失。

中心库使用 ``CenterBase``，租户库使用 ``TenantBase``；两套 ``MetaData`` 分开管理，
分别由 Alembic 的两个迁移目录维护。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Integer, JSON, MetaData, Numeric, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
}

# 表级默认选项：InnoDB + utf8mb4，中文注释由各模型传入
TABLE_OPTIONS: dict[str, str] = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def utcnow() -> datetime:
    """当前 UTC 时间（naive，入库统一 UTC）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class IdMixin:
    """BIGINT 自增主键。"""

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="主键ID",
    )


class TimestampMixin:
    """创建 / 更新时间（UTC）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="创建时间（UTC）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="更新时间（UTC）",
    )


class TenantIdMixin:
    """租户库表统一携带 tenant_id，便于跨租户日志、队列、缓存定位。"""

    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=True,
        comment="租户ID",
    )


class OperatorMixin:
    """操作人字段（可空，自动化任务为空）。"""

    created_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="创建人用户ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="更新人用户ID"
    )


# 常用列类型别名：统一精度，避免各模块自定义
LongId = BigInteger
Money = Numeric(18, 4)
ExchangeRate = Numeric(18, 8)
Quantity = Integer
Payload = JSON


class CenterBase(DeclarativeBase):
    """中心库 Declarative Base。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TenantBase(DeclarativeBase):
    """租户业务库 Declarative Base。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def table_options(comment: str) -> dict[str, str]:
    """生成表级参数：``__table_args__ = table_options("用户表")``。"""
    if not comment:
        raise ValueError("表注释不能为空")
    return {**TABLE_OPTIONS, "comment": comment}


def assert_no_foreign_keys(*metadatas: MetaData) -> None:
    """启动自检：模型层不得出现外键。"""
    offenders: list[str] = []
    for metadata in metadatas:
        for table in metadata.tables.values():
            for fk in table.foreign_keys:
                offenders.append(f"{table.name}.{fk.parent.name}")
    if offenders:
        raise RuntimeError("检测到外键定义（禁止使用外键）: " + ", ".join(sorted(offenders)))


def assert_comments(metadata: MetaData) -> list[str]:
    """返回缺少中文注释的表/字段清单，供校验脚本使用。"""
    problems: list[str] = []
    for table in metadata.tables.values():
        table_comment = (table.comment or "").strip()
        if not _has_chinese(table_comment):
            problems.append(f"表 {table.name} 缺少中文注释")
        for column in table.columns:
            if not _has_chinese((column.comment or "").strip()):
                problems.append(f"字段 {table.name}.{column.name} 缺少中文注释")
    return problems


def _has_chinese(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)
