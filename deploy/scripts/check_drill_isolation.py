#!/usr/bin/env python3
"""隔离恢复演练安全断言：确认 drill compose 解析结果只使用 drill 专属卷/容器。

背景（2026-11-13 事故）：``deploy/docker-compose.yml`` 的顶层卷使用显式
``name: ${COMPOSE_PROJECT_NAME:-ventasync}_mysql_data``，显式卷名会覆盖
``docker compose -p <project>`` 的项目前缀。演练脚本仅加 ``-p ventasync-drill-*``
仍会挂载主项目卷，导致恢复导入写坏主库、清理时删主卷。

本脚本读取 ``docker compose ... config --format json`` 的输出（stdin 或 --config 文件），
断言：

1. 项目名必须是 ``ventasync-drill-<...>``，且显式绑定独立卷名（覆盖 base 中的 name）；
2. 解析后的所有卷名必须以项目名为前缀（因此必然含 "drill"）；
3. mysql / redis 服务挂载的命名卷必须全部属于本项目；
4. 任何解析后的卷名不得等于主项目（``--main-project`` 默认 ventasync）的卷名；
5. mysql 服务的端口发布必须为空（只在内网，不占用生产 3306）；
6. 项目名不得与主项目相同。

退出码：0=安全；1=不安全（打印原因）；2=输入/解析错误。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DRILL_PROJECT_RE = re.compile(r"^ventasync-drill-[a-z0-9][a-z0-9-]*$")
MAIN_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _die(message: str) -> int:
    print(f"[drill-safety] 拒绝：{message}", file=sys.stderr)
    return 1


def _load(path: str | None) -> dict:
    raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("compose config 必须是 JSON 对象")
    return data


def check(config: dict, project: str, main_project: str = "ventasync") -> list[str]:
    """返回问题列表；为空表示安全。"""
    problems: list[str] = []
    if not DRILL_PROJECT_RE.match(project or ""):
        problems.append(f"项目名不合法或不是 drill 项目：{project!r}（应匹配 {DRILL_PROJECT_RE.pattern}）")
    if main_project and project == main_project:
        problems.append("drill 项目名不能与主项目相同")

    config_project = str(config.get("name") or "")
    if config_project and config_project != project:
        problems.append(f"compose 项目名 {config_project!r} 与预期 {project!r} 不一致")

    volumes = config.get("volumes") or {}
    resolved: dict[str, str] = {}
    if not isinstance(volumes, dict):
        problems.append("compose config volumes 不是对象")
        volumes = {}
    for key, spec in volumes.items():
        name = spec.get("name") if isinstance(spec, dict) else None
        if not name:
            problems.append(f"卷 {key} 没有显式 name，可能回退到项目前缀（无法确认隔离）")
            continue
        resolved[key] = str(name)
        if not str(name).startswith(f"{project}_") and not str(name).startswith(project):
            problems.append(f"卷 {key} 解析为 {name!r}，不属于项目 {project}（可能仍挂主卷）")
        if main_project and str(name).startswith(f"{main_project}_"):
            problems.append(f"卷 {key} 解析为 {name!r}，属于主项目 {main_project}")
        if "drill" not in str(name):
            problems.append(f"卷 {key} 解析为 {name!r}，卷名不含 drill，禁止启动")

    services = config.get("services") or {}
    for service in ("mysql", "redis-cache", "redis-queue"):
        svc = services.get(service)
        if not isinstance(svc, dict):
            continue
        for mount in svc.get("volumes") or []:
            source = mount.get("source") if isinstance(mount, dict) else None
            if not source:
                continue
            if source not in resolved:
                problems.append(f"{service} 挂载 {source!r} 未在顶层 volumes 中解析到显式卷名")
                continue
            target = resolved[source]
            if not target.startswith(f"{project}_"):
                problems.append(f"{service} 挂载卷 {source} → {target!r}，不属于项目 {project}")

    mysql = services.get("mysql") or {}
    if isinstance(mysql, dict) and mysql.get("ports"):
        problems.append("mysql 服务仍有端口发布（演练不允许占用宿主机端口）")
    if not isinstance(mysql, dict) or not mysql:
        problems.append("缺少 mysql 服务，无法进行演练")

    mysql_ports = mysql.get("ports") if isinstance(mysql, dict) else None
    if isinstance(mysql, dict) and isinstance(mysql_ports, list) and mysql_ports:
        problems.append(f"mysql 端口发布未清空：{mysql_ports}")

    return problems


def main(argv: list[str]) -> int:
    project = ""
    config_path: str | None = None
    main_project = "ventasync"
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg == "--project" and idx + 1 < len(argv):
            project = argv[idx + 1]; idx += 2
        elif arg == "--config" and idx + 1 < len(argv):
            config_path = argv[idx + 1]; idx += 2
        elif arg == "--main-project" and idx + 1 < len(argv):
            main_project = argv[idx + 1]; idx += 2
        else:
            print(f"[drill-safety] 未知参数：{arg}", file=sys.stderr)
            return 2
    if not project:
        print("[drill-safety] 缺少 --project", file=sys.stderr)
        return 2
    if not MAIN_PROJECT_RE.match(main_project or "ventasync"):
        print(f"[drill-safety] --main-project 不合法：{main_project}", file=sys.stderr)
        return 2
    try:
        config = _load(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[drill-safety] 读取 compose config 失败：{exc}", file=sys.stderr)
        return 2
    problems = check(config, project, main_project)
    if problems:
        for problem in problems:
            print(f"[drill-safety] 拒绝：{problem}", file=sys.stderr)
        return 1
    volumes = sorted(
        str(spec.get("name"))
        for spec in (config.get("volumes") or {}).values()
        if isinstance(spec, dict) and spec.get("name")
    )
    print(json.dumps({"ok": True, "project": project, "volumes": volumes}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
