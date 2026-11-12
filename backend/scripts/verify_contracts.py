#!/usr/bin/env python
"""全量契约校验（离线，不连接 MySQL / Redis，不启动服务）。

校验项（对应 task-8 与 plan.txt §5 验证项 8）：

1. 模型元数据（``app.db.registry``）
   * 中心库 / 租户库均无外键（ForeignKey / ForeignKeyConstraint）
   * 每张表、每个字段都有中文注释
   * 表清单与 ``docs/data-model.md`` 基线完全一致（多表 / 少表都报错）
   * 租户库每张表都有 ``tenant_id``
2. 迁移（``alembic/center``、``alembic/tenant`` 离线 SQL）
   * 迁移文件与 ``docs/data-model.md``「迁移基线」一致，revision 链路连续
   * 离线 SQL 不含 FOREIGN KEY / REFERENCES
   * 离线 SQL 建表清单与基线一致，表与字段均带 COMMENT
3. 权限编码
   * ``docs/api-contract.md`` 权限编码 == ``app.core.permissions.ALL_PERMISSIONS``
   * 预置角色、权限目录、路由中引用的权限码都属于该集合
4. 关键路由
   * ``docs/api-contract.md`` 中的全部「方法 + 路径」在 FastAPI 应用中存在

退出码：全部通过 0，否则 1。输出逐项中文结果，失败项汇总在末尾。

用法（在 backend/ 目录）::

    python scripts/verify_contracts.py
    python scripts/verify_contracts.py --verbose

约束：只读代码与文档；不连接数据库、不调用真实平台接口。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

# 允许 ``python backend/scripts/verify_contracts.py`` 方式运行
BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

DOCS_DIR = REPO_ROOT / "docs"
DATA_MODEL_DOC = DOCS_DIR / "data-model.md"
API_CONTRACT_DOC = DOCS_DIR / "api-contract.md"
CENTER_INI = BACKEND_ROOT / "alembic" / "center.ini"
TENANT_INI = BACKEND_ROOT / "alembic" / "tenant.ini"
CENTER_VERSIONS = BACKEND_ROOT / "alembic" / "center" / "versions"
TENANT_VERSIONS = BACKEND_ROOT / "alembic" / "tenant" / "versions"
MODULES_DIR = BACKEND_ROOT / "app" / "modules"

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
OFFLINE_TENANT_URL = "mysql+pymysql://offline:offline@127.0.0.1:3306/vs_offline"


class Checker:
    """收集校验结果并统一输出。"""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.passed = 0
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def ok(self, label: str, detail: str = "") -> None:
        self.passed += 1
        print(f"[通过] {label}" + (f"：{detail}" if detail else ""))

    def fail(self, label: str, detail: str = "") -> None:
        message = f"{label}：{detail}" if detail else label
        self.failures.append(message)
        print(f"[失败] {message}")

    def warn(self, label: str, detail: str = "") -> None:
        message = f"{label}：{detail}" if detail else label
        self.warnings.append(message)
        print(f"[警告] {message}")

    def check(self, condition: bool, label: str, detail: str = "") -> bool:
        if condition:
            self.ok(label, detail)
        else:
            self.fail(label, detail)
        return condition

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")

    def summary(self) -> int:
        print("\n" + "=" * 72)
        print(f"通过 {self.passed} 项，失败 {len(self.failures)} 项，警告 {len(self.warnings)} 项")
        if self.failures:
            print("\n失败明细：")
            for index, item in enumerate(self.failures, start=1):
                print(f"  {index}. {item}")
            print("\n结果：未通过（请修复后重跑 python scripts/verify_contracts.py）")
            return 1
        print("\n结果：全部通过")
        return 0


# --------------------------------------------------------------------------- 文档解析
def _read(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"缺少契约文档：{path}")
    return path.read_text(encoding="utf-8")


def parse_expected_tables(text: str) -> tuple[set[str], set[str]]:
    """从 docs/data-model.md 解析中心库 / 租户库的表清单。"""
    try:
        center_start = text.index("## 中心库")
        tenant_start = text.index("## 租户库")
        migration_start = text.index("## 迁移基线")
    except ValueError as exc:  # pragma: no cover - 文档结构被破坏时给出明确错误
        raise ValueError("docs/data-model.md 缺少「中心库 / 租户库 / 迁移基线」章节") from exc
    center_text = text[center_start:tenant_start]
    tenant_text = text[tenant_start:migration_start]
    table_row = re.compile(r"^\|\s*`([a-z][a-z0-9_]*)`\s*\|", re.MULTILINE)
    return set(table_row.findall(center_text)), set(table_row.findall(tenant_text))


def parse_migration_doc(text: str) -> list[tuple[str, str, str]]:
    """解析「迁移基线」表：[(数据库, 文件, revision), ...]，保持文档顺序。"""
    section = text[text.index("## 迁移基线") :]
    entries: list[tuple[str, str, str]] = []
    for line in section.splitlines():
        if ".py" not in line:
            continue
        # 表格列：数据库 | `迁移文件` | revision | 内容
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        file_index = next(
            (index for index, cell in enumerate(cells) if cell.strip("`").endswith(".py")),
            None,
        )
        if file_index is None:
            continue
        file_token = cells[file_index].strip("`")
        revision = (
            cells[file_index + 1].strip("`") if file_index + 1 < len(cells) else ""
        )
        database = "center" if "/center/" in file_token else "tenant"
        entries.append((database, file_token, revision))
    return entries


def parse_docs_permissions(text: str) -> frozenset[str]:
    """解析「权限编码」章节，展开 ``a.b.read/create`` 这类简写。"""
    start = text.index("## 权限编码")
    end = text.find("\n## ", start + 1)
    block = text[start : end if end != -1 else len(text)]
    codes: set[str] = set()
    for token in re.findall(r"`([^`]+)`", block):
        token = token.strip()
        if not token or token.startswith("/"):
            continue
        parts = token.split("/")
        first = parts[0]
        if not re.fullmatch(r"[a-z][a-z0-9_.]+", first):
            continue
        codes.add(first)
        if "." in first:
            prefix = first.rsplit(".", 1)[0]
            for part in parts[1:]:
                if re.fullmatch(r"[a-z][a-z0-9_]*", part):
                    codes.add(f"{prefix}.{part}")
    return frozenset(codes)


def parse_docs_routes(text: str) -> set[tuple[str, str]]:
    """解析 API 契约表格中的「方法 + 路径」，去掉查询串并归一化路径参数。"""
    expected: set[tuple[str, str]] = set()
    row_pattern = re.compile(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|(.*)$")
    token_pattern = re.compile(r"`([^`]+)`")
    for line in text.splitlines():
        row = row_pattern.match(line)
        if not row:
            continue
        default_method = row.group(1)
        for token in token_pattern.findall(row.group(2)):
            token = token.strip()
            explicit = re.match(r"^(GET|POST|PUT|PATCH|DELETE)\s+(/.*)$", token)
            if explicit:
                method, path = explicit.group(1), explicit.group(2)
            elif token.startswith("/"):
                method, path = default_method, token
            else:
                continue
            path = path.split("?")[0].rstrip("/") or "/"
            expected.add((method, normalize_path(path)))
    return expected


def normalize_path(path: str) -> str:
    """路径参数名不在契约层面约束，统一归一化为 ``{}`` 后比较。"""
    return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/") or "/")


# --------------------------------------------------------------------------- 迁移 SQL
def run_alembic_offline(ini: Path, extra_args: list[str] | None = None) -> tuple[int, str]:
    """调用 alembic 离线模式生成 SQL；返回 (退出码, 合并输出)。"""
    command = [sys.executable, "-m", "alembic", "-c", str(ini)]
    if extra_args:
        command.extend(extra_args)
    command.extend(["upgrade", "head", "--sql"])
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(
            command,
            cwd=str(BACKEND_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - 环境异常
        return 1, f"执行 alembic 失败：{exc}"
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output


CREATE_TABLE_RE = re.compile(
    # 表体使用 tempered 模式，避免把 alembic_version（无 ENGINE）与下一条建表一起吞掉
    r"CREATE TABLE (\w+) \(((?:(?!CREATE TABLE).)*?)\)\s*"
    r"((?:ENGINE=|COMMENT=|CHARSET=|COLLATE\b|AUTO_INCREMENT\b)[^;]*);",
    re.DOTALL,
)
CONSTRAINT_PREFIXES = (
    "CONSTRAINT ",
    "PRIMARY KEY",
    "UNIQUE KEY",
    "UNIQUE (",
    "KEY ",
    "INDEX ",
    "FULLTEXT ",
    "CHECK ",
)


def parse_sql_tables(sql: str) -> dict[str, dict[str, object]]:
    """解析 CREATE 以及后续 ADD/MODIFY/DROP，检查最终模式，防止 MODIFY 清除注释。"""
    tables: dict[str, dict[str, object]] = {}
    for match in CREATE_TABLE_RE.finditer(sql):
        name, body, options = match.group(1), match.group(2), match.group(3)
        comment_match = re.search(r"COMMENT='([^']*)'", options)
        columns: dict[str, str] = {}
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line or line.upper().startswith(CONSTRAINT_PREFIXES):
                continue
            column = re.match(r"^`?([a-z_][a-z0-9_]*)`?\s", line)
            if column:
                comment = re.search(r" COMMENT '([^']*)'", line)
                columns[column.group(1)] = comment.group(1) if comment else ""
        tables[name] = {"comment": comment_match.group(1) if comment_match else "", "columns": columns}
    alterations = re.compile(r"ALTER TABLE `?(\w+)`? (ADD COLUMN|MODIFY(?: COLUMN)?|DROP COLUMN) `?(\w+)`?([^;]*);", re.DOTALL | re.IGNORECASE)
    for alteration in alterations.finditer(sql):
        name, action, column, definition = alteration.groups()
        if name not in tables:
            continue
        columns = tables[name]["columns"]
        if action.upper() == "DROP COLUMN":
            columns.pop(column, None)
        else:
            comment = re.search(r" COMMENT '([^']*)'", definition)
            columns[column] = comment.group(1) if comment else ""
    for info in tables.values():
        columns = info["columns"]
        info["missing_columns"] = [name for name, comment in columns.items() if not comment]
        info["non_chinese_columns"] = [name for name, comment in columns.items() if comment and not has_chinese(comment)]
    return tables


def has_chinese(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def check_migration_sql(checker: Checker, label: str, sql: str, expected_tables: set[str]) -> None:
    """迁移 SQL 的零外键、表清单、表/字段中文注释校验。"""
    if re.search(r"(?i)\bFOREIGN\s+KEY\b", sql):
        checker.fail(f"{label}离线 SQL 零外键", "检测到 FOREIGN KEY 语句")
    elif re.search(r"(?i)\bREFERENCES\b", sql):
        checker.fail(f"{label}离线 SQL 零外键", "检测到 REFERENCES 语句")
    else:
        checker.ok(f"{label}离线 SQL 零外键", "无 FOREIGN KEY / REFERENCES")

    sql_tables = parse_sql_tables(sql)
    missing_tables = sorted(expected_tables - set(sql_tables))
    extra_tables = sorted(set(sql_tables) - expected_tables)
    checker.check(
        not missing_tables and not extra_tables,
        f"{label}迁移建表清单与 docs/data-model.md 一致",
        f"共 {len(sql_tables)} 张"
        + (f"；缺少 {missing_tables}" if missing_tables else "")
        + (f"；多出 {extra_tables}" if extra_tables else ""),
    )

    # 比较最终列集合，避免新增模型字段没有迁移、或 ADD/MODIFY 未纳入基线检查。
    from app.db.registry import center_metadata, tenant_metadata

    metadata = center_metadata if label == "中心库" else tenant_metadata
    column_differences: list[str] = []
    for name in sorted(expected_tables & set(sql_tables) & set(metadata.tables)):
        expected_columns = set(metadata.tables[name].columns.keys())
        actual_columns = set(sql_tables[name]["columns"])
        if expected_columns != actual_columns:
            column_differences.append(
                f"{name}: 缺少={sorted(expected_columns - actual_columns)}, "
                f"多出={sorted(actual_columns - expected_columns)}"
            )
    checker.check(
        not column_differences,
        f"{label}最终迁移列与模型一致",
        "；".join(column_differences),
    )

    table_comment_problems = [
        name for name, info in sql_tables.items() if not has_chinese(str(info["comment"]))
    ]
    checker.check(
        not table_comment_problems,
        f"{label}迁移表注释为中文",
        f"缺少中文注释的表：{table_comment_problems}" if table_comment_problems else "",
    )

    missing_columns = {
        name: info["missing_columns"]
        for name, info in sql_tables.items()
        if info["missing_columns"]
    }
    checker.check(
        not missing_columns,
        f"{label}迁移字段注释完整",
        f"缺注释字段：{missing_columns}" if missing_columns else "",
    )

    non_chinese_columns = {
        name: info["non_chinese_columns"]
        for name, info in sql_tables.items()
        if info["non_chinese_columns"]
    }
    checker.check(
        not non_chinese_columns,
        f"{label}迁移字段注释为中文",
        f"非中文注释字段：{non_chinese_columns}" if non_chinese_columns else "",
    )


# --------------------------------------------------------------------------- 元数据
def check_metadata(
    checker: Checker,
    label: str,
    metadata,
    expected_tables: set[str],
    *,
    require_tenant_id: bool,
) -> None:
    """模型元数据：表清单、外键、中文注释、tenant_id。"""
    from sqlalchemy.schema import ForeignKeyConstraint

    actual_tables = set(metadata.tables)
    missing = sorted(expected_tables - actual_tables)
    extra = sorted(actual_tables - expected_tables)
    checker.check(
        not missing and not extra,
        f"{label}元数据表清单与 docs/data-model.md 一致",
        f"共 {len(actual_tables)} 张"
        + (f"；缺少 {missing}" if missing else "")
        + (f"；多出 {extra}" if extra else ""),
    )

    foreign_key_problems: list[str] = []
    for table in metadata.tables.values():
        for column in table.columns:
            if column.foreign_keys:
                foreign_key_problems.append(f"{table.name}.{column.name}")
        for constraint in table.constraints:
            if isinstance(constraint, ForeignKeyConstraint):
                foreign_key_problems.append(f"{table.name}({constraint.name})")
    checker.check(
        not foreign_key_problems,
        f"{label}元数据零外键",
        f"检测到外键：{sorted(set(foreign_key_problems))}" if foreign_key_problems else "",
    )

    from app.db.base import assert_comments

    comment_problems = assert_comments(metadata)
    checker.check(
        not comment_problems,
        f"{label}表与字段中文注释完整",
        f"{len(comment_problems)} 处缺失，例如 {comment_problems[:5]}" if comment_problems else "",
    )

    if require_tenant_id:
        missing_tenant_id = sorted(
            name for name, table in metadata.tables.items() if "tenant_id" not in table.columns
        )
        checker.check(
            not missing_tenant_id,
            f"{label}每张表包含 tenant_id",
            f"缺少 tenant_id：{missing_tenant_id}" if missing_tenant_id else "",
        )

    # docs/data-model.md 约定：每张表都有 BIGINT 自增 id、created_at、updated_at（UTC DATETIME）
    from sqlalchemy import BigInteger, DateTime

    standard_problems: list[str] = []
    for name, table in metadata.tables.items():
        id_column = table.columns.get("id")
        if id_column is None:
            standard_problems.append(f"{name} 缺少 id")
        elif not isinstance(id_column.type, BigInteger) or not id_column.primary_key:
            standard_problems.append(f"{name}.id 不是 BIGINT 主键")
        for column_name in ("created_at", "updated_at"):
            column = table.columns.get(column_name)
            if column is None:
                standard_problems.append(f"{name} 缺少 {column_name}")
            elif not isinstance(column.type, DateTime):
                standard_problems.append(f"{name}.{column_name} 不是 DATETIME")
    checker.check(
        not standard_problems,
        f"{label}每张表包含 id/created_at/updated_at",
        f"{standard_problems[:8]}" if standard_problems else "",
    )


# --------------------------------------------------------------------------- 迁移文件 / revision
def check_migration_files(checker: Checker, entries: list[tuple[str, str, str]]) -> None:
    """从 Alembic 实际版本图发现全部迁移；文档只作为对照，不能决定校验范围。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    actual: list[tuple[str, str, str]] = []
    heads: dict[str, str] = {}
    for database, ini in (("center", CENTER_INI), ("tenant", TENANT_INI)):
        try:
            script = ScriptDirectory.from_config(Config(str(ini)))
            discovered_heads = script.get_heads()
            if len(discovered_heads) != 1:
                checker.fail(f"{database} 单一迁移末端", str(discovered_heads))
                continue
            revisions = list(reversed(list(script.walk_revisions(base="base", head="heads"))))
            previous = None
            for item in revisions:
                if item.down_revision != previous:
                    raise ValueError(f"{item.revision}: down_revision={item.down_revision!r}, expected={previous!r}")
                relative = Path(item.path).resolve().relative_to(BACKEND_ROOT).as_posix()
                actual.append((database, relative, item.revision))
                previous = item.revision
            heads[database] = discovered_heads[0]
        except Exception as exc:
            checker.fail(f"{database} 实际迁移图可读取", str(exc))
    checker.check(bool(actual), "实际迁移发现", f"{len(actual)} 个迁移文件")
    documented = set(entries)
    discovered = set(actual)
    checker.check(
        len(entries) == len(documented) and documented == discovered,
        "迁移文档覆盖实际版本图",
        f"遗漏={sorted(discovered - documented)}；过时={sorted(documented - discovered)}"
        if documented != discovered else f"共 {len(actual)} 个迁移文件",
    )
    if heads:
        checker.ok("实际迁移链末端", "，".join(f"{key}: {value}" for key, value in sorted(heads.items())))
    source_problems: list[str] = []
    for versions_dir in (CENTER_VERSIONS, TENANT_VERSIONS):
        for path in sorted(versions_dir.glob("*.py")):
            content = path.read_text(encoding="utf-8")
            if re.search(r"\bForeignKey(?:Constraint)?\b|\bcreate_foreign_key\b", content):
                source_problems.append(path.name)
    checker.check(not source_problems, "全部迁移源码零外键", str(source_problems) if source_problems else "")


# --------------------------------------------------------------------------- 权限
def check_permissions(checker: Checker, docs_text: str) -> None:
    from app.core.permissions import ALL_PERMISSIONS, PREDEFINED_ROLE_PERMISSIONS

    documented = parse_docs_permissions(docs_text)
    checker.check(
        bool(documented),
        "docs/api-contract.md 权限编码解析",
        f"共 {len(documented)} 个" if documented else "未解析到权限编码",
    )
    checker.check(
        documented == set(ALL_PERMISSIONS),
        "权限编码与 docs/api-contract.md 一致",
        "仅文档有："
        + str(sorted(documented - set(ALL_PERMISSIONS)))
        + "；仅代码有："
        + str(sorted(set(ALL_PERMISSIONS) - documented))
        if documented != set(ALL_PERMISSIONS)
        else f"共 {len(ALL_PERMISSIONS)} 个",
    )

    try:
        from app.modules.identity.permission_catalog import (
            missing_catalogue_codes,
            unknown_catalogue_codes,
        )

        missing_codes = missing_catalogue_codes()
        unknown_codes = unknown_catalogue_codes()
        checker.check(
            not missing_codes and not unknown_codes,
            "权限目录与 ALL_PERMISSIONS 一致",
            f"未编入目录：{sorted(missing_codes)}；目录多出：{sorted(unknown_codes)}"
            if missing_codes or unknown_codes
            else "",
        )
    except Exception as exc:  # noqa: BLE001 - 模块未就绪时给出明确失败
        checker.fail("权限目录与 ALL_PERMISSIONS 一致", f"无法导入 permission_catalog：{exc}")

    role_problems = [
        f"{role}:{code}"
        for role, codes in PREDEFINED_ROLE_PERMISSIONS.items()
        for code in codes
        if code not in ALL_PERMISSIONS
    ]
    checker.check(
        not role_problems,
        "预置角色权限编码有效",
        f"未知编码：{role_problems}" if role_problems else "",
    )

    literal_pattern = re.compile(r"require_(?:any_)?permission\(\s*[\"']([a-z][a-z0-9_.]+)[\"']")
    literal_problems: list[str] = []
    for path in sorted(MODULES_DIR.rglob("*.py")):
        content = path.read_text(encoding="utf-8")
        for code in literal_pattern.findall(content):
            if code not in ALL_PERMISSIONS:
                literal_problems.append(f"{path.relative_to(BACKEND_ROOT)}:{code}")
    checker.check(
        not literal_problems,
        "路由引用的权限码均已定义",
        f"未知权限码：{literal_problems}" if literal_problems else "",
    )


# --------------------------------------------------------------------------- 路由
def check_routes(checker: Checker, docs_text: str) -> None:
    expected = parse_docs_routes(docs_text)
    checker.check(
        len(expected) >= 30,
        "docs/api-contract.md 路由解析",
        f"共 {len(expected)} 条" if expected else "未解析到路由",
    )

    try:
        from fastapi.routing import APIRoute

        from app.core.config import settings
        from app.main import app
    except Exception as exc:  # noqa: BLE001 - 应用模块未完成时给出明确失败
        checker.fail(
            "FastAPI 应用可导入",
            f"{exc.__class__.__name__}: {exc}（模块尚未完成时属预期，待全部模块就绪后重跑）",
        )
        if checker.verbose:
            traceback.print_exc()
        return
    checker.ok("FastAPI 应用可导入", f"共 {len(app.routes)} 条路由对象")

    prefix = settings.api_prefix.rstrip("/")
    actual: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            actual.add((method, normalize_path(route.path)))
    missing = sorted(
        (method, path)
        for method, path in expected
        if (method, normalize_path(prefix + path)) not in actual
    )
    checker.check(
        not missing,
        "docs/api-contract.md 关键路由全部存在",
        f"{len(expected)} 条中缺少 {len(missing)} 条：{missing[:12]}"
        if missing
        else f"共 {len(expected)} 条",
    )


# --------------------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ERP 离线契约校验")
    parser.add_argument("--verbose", action="store_true", help="输出异常堆栈等调试信息")
    args = parser.parse_args(argv)

    # 固定 UTF-8 输出，避免 Windows 控制台代码页导致中文乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - 非常规 stdout
        pass

    checker = Checker(verbose=args.verbose)

    checker.section("契约文档")
    try:
        data_model_text = _read(DATA_MODEL_DOC)
        api_contract_text = _read(API_CONTRACT_DOC)
    except FileNotFoundError as exc:
        checker.fail("契约文档存在", str(exc))
        return checker.summary()
    checker.ok("契约文档存在", "docs/data-model.md、docs/api-contract.md")
    try:
        expected_center, expected_tenant = parse_expected_tables(data_model_text)
        migration_entries = parse_migration_doc(data_model_text)
    except ValueError as exc:
        checker.fail("契约文档结构", str(exc))
        return checker.summary()
    checker.ok(
        "数据模型基线解析",
        f"中心库 {len(expected_center)} 张表，租户库 {len(expected_tenant)} 张表",
    )

    checker.section("模型元数据")
    try:
        from app.db.registry import center_metadata, tenant_metadata
    except Exception as exc:  # noqa: BLE001 - 模型未完成时给出明确失败
        checker.fail(
            "模型元数据可导入",
            f"{exc.__class__.__name__}: {exc}（模块尚未完成时属预期，待全部模块就绪后重跑）",
        )
        if checker.verbose:
            traceback.print_exc()
        return checker.summary()
    checker.ok("模型元数据可导入")
    check_metadata(
        checker, "中心库", center_metadata, expected_center, require_tenant_id=False
    )
    check_metadata(
        checker, "租户库", tenant_metadata, expected_tenant, require_tenant_id=True
    )

    checker.section("迁移文件与离线 SQL")
    check_migration_files(checker, migration_entries)

    center_code, center_sql = run_alembic_offline(CENTER_INI)
    if center_code != 0:
        checker.fail(
            "中心库离线 SQL 生成",
            f"alembic 退出码 {center_code}：{center_sql.strip().splitlines()[-3:]}",
        )
    else:
        checker.ok("中心库离线 SQL 生成", f"{len(center_sql.splitlines())} 行")
        check_migration_sql(checker, "中心库", center_sql, expected_center)

    tenant_code, tenant_sql = run_alembic_offline(TENANT_INI, ["-x", f"db_url={OFFLINE_TENANT_URL}"])
    if tenant_code != 0:
        checker.fail(
            "租户库离线 SQL 生成",
            f"alembic 退出码 {tenant_code}：{tenant_sql.strip().splitlines()[-3:]}",
        )
    else:
        checker.ok("租户库离线 SQL 生成", f"{len(tenant_sql.splitlines())} 行")
        check_migration_sql(checker, "租户库", tenant_sql, expected_tenant)

    checker.section("权限编码")
    check_permissions(checker, api_contract_text)

    checker.section("关键路由")
    check_routes(checker, api_contract_text)

    return checker.summary()


if __name__ == "__main__":
    raise SystemExit(main())
