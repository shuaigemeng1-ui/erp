#!/usr/bin/env python3
"""Inventory a trusted mysqldump before importing into an isolated drill MySQL.

This is a format/target guard, not a general SQL security sandbox. It recognizes
mysqldump statements (including executable version comments and view stand-ins),
ignores strings/comments, and never emits row data or SQL text. Do not use it to
authorize importing an untrusted third-party SQL file into a privileged server.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
SYSTEM_SCHEMAS = {"mysql", "sys", "information_schema", "performance_schema"}
Token = tuple[str, str, int]


class DumpError(ValueError):
    pass


class Inventory:
    def __init__(self, database: str):
        if not IDENTIFIER.fullmatch(database) or database.lower() in SYSTEM_SCHEMAS:
            raise DumpError("目标 schema 名称无效或属于系统库")
        self.database = database
        self.objects: dict[str, str] = {}
        self.created_tables: set[str] = set()
        self.declarations: list[dict] = []

    @staticmethod
    def _word(token: Token, value: str) -> bool:
        return token[0] == "word" and token[1].upper() == value

    @staticmethod
    def _identifier(tokens: list[Token], index: int) -> tuple[str, int]:
        if index >= len(tokens) or tokens[index][0] not in {"word", "identifier"}:
            raise DumpError("DDL 中缺少合法标识符")
        value = tokens[index][1]
        if not IDENTIFIER.fullmatch(value):
            raise DumpError("DDL 标识符只允许字母、数字、下划线")
        return value, index + 1

    def _target(self, tokens: list[Token], index: int) -> tuple[str, int]:
        name, index = self._identifier(tokens, index)
        if index < len(tokens) and tokens[index][1] == ".":
            self._check_database(name)
            name, index = self._identifier(tokens, index + 1)
        return name, index

    def _check_database(self, name: str) -> None:
        if name != self.database or name.lower() in SYSTEM_SCHEMAS:
            raise DumpError("dump 声明了非预期 schema，拒绝跨库导入")

    def _skip_if(self, tokens: list[Token], index: int, create: bool) -> int:
        expected = ["IF", "NOT", "EXISTS"] if create else ["IF", "EXISTS"]
        if index < len(tokens) and self._word(tokens[index], "IF"):
            if len(tokens) < index + len(expected) or any(
                not self._word(tokens[index + offset], word)
                for offset, word in enumerate(expected)
            ):
                raise DumpError("无法识别 DDL 的 IF EXISTS 子句")
            index += len(expected)
        return index

    def statement(self, tokens: list[Token]) -> None:
        if not tokens:
            return
        line = tokens[0][2]
        try:
            if self._word(tokens[0], "USE"):
                name, end = self._identifier(tokens, 1)
                if end != len(tokens):
                    raise DumpError("无法识别 USE 声明")
                self._check_database(name)
                self.declarations.append({"kind": "USE", "database": name, "line": line})
                return
            action = tokens[0][1].upper() if tokens[0][0] == "word" else ""
            if action not in {"CREATE", "DROP", "ALTER", "RENAME"}:
                return
            if len(tokens) < 2:
                raise DumpError("DDL 声明不完整")
            kind = tokens[1][1].upper() if tokens[1][0] == "word" else ""
            if kind in {"DATABASE", "SCHEMA"}:
                index = self._skip_if(tokens, 2, action == "CREATE")
                name, _ = self._identifier(tokens, index)
                self._check_database(name)
                self.declarations.append({"kind": f"{action} {kind}", "database": name, "line": line})
                if action == "DROP":
                    self.objects.clear()
                return
            if action == "RENAME" and kind == "TABLE":
                raise DumpError("不支持 mysqldump 之外的 RENAME TABLE")
            if kind == "TEMPORARY":
                raise DumpError("不支持临时表 DDL")
            if kind == "TABLE" and action in {"CREATE", "DROP"}:
                index = self._skip_if(tokens, 2, action == "CREATE")
                name, index = self._target(tokens, index)
                if action == "CREATE":
                    if name in self.created_tables or name in self.objects:
                        raise DumpError("dump 含重复 CREATE TABLE")
                    self.created_tables.add(name)
                    self.objects[name] = "table"
                else:
                    self.objects.pop(name, None)
                    while index < len(tokens) and tokens[index][1] == ",":
                        name, index = self._target(tokens, index + 1)
                        self.objects.pop(name, None)
                return
            if kind == "VIEW" and action == "DROP":
                name, _ = self._target(tokens, self._skip_if(tokens, 2, False))
                self.objects.pop(name, None)
                return
            if action == "CREATE":
                # A mysqldump view may start with ALGORITHM / DEFINER / SQL SECURITY.
                # Stop at AS so words inside SELECT cannot become a declaration.
                for index, token in enumerate(tokens[1:], 1):
                    if any(self._word(token, word) for word in ("AS", "PROCEDURE", "FUNCTION", "TRIGGER", "EVENT")):
                        break
                    if self._word(token, "VIEW"):
                        name, _ = self._target(tokens, index + 1)
                        if self.objects.get(name) == "table":
                            raise DumpError("CREATE VIEW 前的占位表未被 DROP")
                        self.objects[name] = "view"
                        return
        except DumpError as exc:
            raise DumpError(f"第 {line} 行：{exc}") from None

    def result(self, digest: str) -> dict:
        tables = sorted(name for name, kind in self.objects.items() if kind == "table")
        if not tables:
            raise DumpError("dump 没有可恢复的 BASE TABLE")
        return {
            "database": self.database,
            "table_names": tables,
            "table_count": len(tables),
            "create_table_count": len(self.created_tables),
            "view_names": sorted(name for name, kind in self.objects.items() if kind == "view"),
            "schema_declarations": self.declarations,
            "sql_sha256": digest,
        }


class Lexer:
    """Bounded statement-prefix lexer; row values are discarded, never reported."""

    def __init__(self, inventory: Inventory):
        self.inventory = inventory
        self.tokens: list[Token] = []
        self.state = "normal"
        self.version_comment = False
        self.delimiter = ";"
        self.quote = ""
        self.value = ""
        self.start_line = 0
        self.escape = False

    def emit(self, kind: str, value: str, line: int) -> None:
        # CREATE VIEW metadata fits comfortably; no need to retain INSERT rows.
        if len(self.tokens) < 96:
            self.tokens.append((kind, value, line))

    def feed(self, text: str, line: int) -> None:
        if self.state == "normal" and not self.tokens and not self.version_comment:
            delimiter = re.fullmatch(r"\s*DELIMITER\s+(\S+)\s*", text, re.I)
            if delimiter:
                self.delimiter = delimiter.group(1)
                return
        i = 0
        while i < len(text):
            char = text[i]
            if self.state == "comment":
                end = text.find("*/", i)
                if end < 0:
                    return
                self.state = "normal"
                i = end + 2
                continue
            if self.state in {"string", "identifier"}:
                if self.escape:
                    self.escape = False
                    if self.state == "identifier":
                        self.value += char
                    i += 1
                    continue
                if char == "\\":
                    self.escape = True
                    i += 1
                    continue
                if char == self.quote:
                    if i + 1 < len(text) and text[i + 1] == self.quote:
                        if self.state == "identifier":
                            self.value += char
                        i += 2
                        continue
                    self.emit(self.state, self.value if self.state == "identifier" else "", self.start_line)
                    self.state = "normal"
                    self.value = ""
                elif self.state == "identifier":
                    self.value += char
                    if len(self.value) > 128:
                        raise DumpError(f"第 {self.start_line} 行：标识符过长")
                i += 1
                continue
            if self.version_comment and text.startswith("*/", i):
                self.version_comment = False
                i += 2
                continue
            if text.startswith("/*!", i):
                if self.version_comment:
                    raise DumpError(f"第 {line} 行：不支持嵌套版本注释")
                self.version_comment = True
                i += 3
                while i < len(text) and text[i].isdigit():
                    i += 1
                continue
            if text.startswith("/*", i):
                self.state = "comment"
                i += 2
                continue
            if char == "#" or (text.startswith("--", i) and (i + 2 == len(text) or text[i + 2].isspace())):
                return
            if char.isspace():
                i += 1
                continue
            if text.startswith(self.delimiter, i):
                self.inventory.statement(self.tokens)
                self.tokens = []
                i += len(self.delimiter)
                continue
            if char in "'\"`":
                self.state = "identifier" if char == "`" else "string"
                self.quote = char
                self.start_line = line
                self.value = ""
                i += 1
                continue
            if char.isalnum() or char in "_$":
                end = i + 1
                while end < len(text) and (text[end].isalnum() or text[end] in "_$"):
                    end += 1
                self.emit("word", text[i:min(end, i + 128)], line)
                i = end
                continue
            self.emit("symbol", char, line)
            i += 1

    def finish(self) -> None:
        if self.state != "normal" or self.version_comment:
            raise DumpError("dump 末尾存在未闭合字符串或注释")
        if self.tokens:
            raise DumpError("dump 末尾存在未终止 SQL 语句")


def inspect_dump(path: Path, database: str, sql_output: Path | None = None) -> dict:
    inventory = Inventory(database)
    lexer = Lexer(inventory)
    digest = hashlib.sha256()
    temporary: str | None = None
    output = None
    try:
        if sql_output:
            fd, temporary = tempfile.mkstemp(prefix=".drill-sql-", dir=sql_output.parent)
            os.chmod(temporary, 0o600)
            output = os.fdopen(fd, "wb")
        with gzip.open(path, "rb") as source:
            for line_number, raw_line in enumerate(source, 1):
                digest.update(raw_line)
                if output:
                    output.write(raw_line)
                lexer.feed(raw_line.decode("utf-8", errors="strict"), line_number)
        lexer.finish()
        result = inventory.result(digest.hexdigest())
        if output:
            output.close()
            output = None
            os.replace(temporary, sql_output)
            temporary = None
        return result
    finally:
        if output:
            output.close()
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sql-output", type=Path)
    args = parser.parse_args()
    try:
        result = inspect_dump(args.file, args.database, args.sql_output)
        fd, temporary = tempfile.mkstemp(prefix=".drill-inventory-", dir=args.output.parent)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as destination:
                json.dump(result, destination, ensure_ascii=False, indent=2)
                destination.write("\n")
            os.replace(temporary, args.output)
        finally:
            Path(temporary).unlink(missing_ok=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, EOFError, UnicodeError, DumpError) as exc:
        # Decoder exceptions can include input byte fragments; don't emit them.
        detail = str(exc) if isinstance(exc, DumpError) else type(exc).__name__
        print(f"[drill-dump] 拒绝：{detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
