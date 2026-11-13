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
3. mysql / redis 服务只允许本项目的 local 命名卷，禁止 bind / external / driver_opts；
4. 任何解析后的卷名不得等于主项目（``--main-project`` 默认 ventasync）的卷名；
5. mysql 服务的端口发布必须为空（只在内网，不占用生产 3306）；
6. 项目名不得与主项目相同；mysql 数据目录必须是唯一的可写命名卷；
7. mysql / redis 禁止固定容器名、host/container 网络及 volumes_from。

退出码：0=安全；1=不安全（打印原因）；2=输入/解析错误。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DRILL_PROJECT_RE = re.compile(r"^ventasync-drill-[a-z0-9][a-z0-9-]*$")
MAIN_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ISOLATED_SERVICES = ("mysql", "redis-cache", "redis-queue")


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
    if not MAIN_PROJECT_RE.fullmatch(main_project or ""):
        problems.append("主项目名不合法")

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
        if not str(name).startswith(f"{project}_"):
            problems.append(f"卷 {key} 解析为 {name!r}，不属于项目 {project}（可能仍挂主卷）")
        if main_project and str(name).startswith(f"{main_project}_"):
            problems.append(f"卷 {key} 解析为 {name!r}，属于主项目 {main_project}")
        if "drill" not in str(name):
            problems.append(f"卷 {key} 解析为 {name!r}，卷名不含 drill，禁止启动")
        if spec.get("external"):
            problems.append(f"卷 {key} 是 external 卷，禁止复用现有外部卷")
        if spec.get("driver_opts"):
            problems.append(f"卷 {key} 含 driver_opts，可能指向主库目录或远程存储")
        if spec.get("driver") not in (None, "", "local"):
            problems.append(f"卷 {key} 必须使用 local driver")

    services = config.get("services") or {}
    if not isinstance(services, dict):
        problems.append("compose config services 不是对象")
        services = {}
    for service in ISOLATED_SERVICES:
        svc = services.get(service)
        if not isinstance(svc, dict):
            continue
        if svc.get("container_name"):
            problems.append(f"{service} 禁止固定 container_name")
        if svc.get("network_mode"):
            problems.append(f"{service} 禁止 network_mode，必须使用独立 compose 网络")
        if svc.get("volumes_from"):
            problems.append(f"{service} 禁止 volumes_from")
        if svc.get("privileged") or svc.get("devices"):
            problems.append(f"{service} 禁止 privileged/devices")
        mounts = svc.get("volumes") or []
        if not isinstance(mounts, list):
            problems.append(f"{service} volumes 不是列表")
            continue
        for mount in mounts:
            if not isinstance(mount, dict) or mount.get("type") != "volume":
                problems.append(f"{service} 仅允许显式 volume 挂载，禁止 bind/tmpfs/匿名挂载")
                continue
            source = mount.get("source")
            if source not in resolved:
                problems.append(f"{service} 挂载 {source!r} 未在顶层 volumes 中解析到显式卷名")
                continue
            target = resolved[source]
            if not target.startswith(f"{project}_"):
                problems.append(f"{service} 挂载卷 {source} → {target!r}，不属于项目 {project}")
        if service == "mysql":
            if len(mounts) != 1:
                problems.append("mysql 必须且只能挂载一个 /var/lib/mysql 数据卷")
            elif not isinstance(mounts[0], dict) or mounts[0].get("target") != "/var/lib/mysql" or mounts[0].get("read_only", False) is not False:
                problems.append("mysql 数据卷必须可写且挂载到 /var/lib/mysql")
            if svc.get("tmpfs"):
                problems.append("mysql 禁止额外 tmpfs 挂载")

    mysql = services.get("mysql") or {}
    if isinstance(mysql, dict) and mysql.get("ports"):
        problems.append("mysql 服务仍有端口发布（演练不允许占用宿主机端口）")
    if not isinstance(mysql, dict) or not mysql:
        problems.append("缺少 mysql 服务，无法进行演练")

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
