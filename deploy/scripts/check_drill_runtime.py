#!/usr/bin/env python3
"""只读校验 Docker inspect 快照；只允许新 drill MySQL 使用独立的普通 local 卷。

调用方通过 docker inspect / docker volume inspect 收集 JSON，本脚本不执行 Docker。
--container/--volume 必须各包含一个对象；--main-container 必须包含主 MySQL。
退出码：0=安全；1=隔离校验失败；2=输入错误。stdout 不输出环境变量/密码。
"""

from __future__ import annotations

import argparse
import json
import posixpath
import sys
from pathlib import Path

from check_drill_isolation import check


def load_object(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        if len(data) != 1:
            raise ValueError(f"{path}: 必须恰好包含一个 inspect 对象")
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 必须是 JSON 对象或单元素数组")
    return data


def _datadir_mounts(container: dict) -> list[dict]:
    return [m for m in container.get("Mounts", []) if isinstance(m, dict) and m.get("Destination") == "/var/lib/mysql"]


def check_runtime(config: dict, project: str, main_project: str, container: dict, volume: dict, main: dict) -> list[str]:
    problems = check(config, project, main_project)
    if problems:
        return problems

    labels = (container.get("Config") or {}).get("Labels") or {}
    main_labels = (main.get("Config") or {}).get("Labels") or {}
    for label, expected in (("com.docker.compose.project", project), ("com.docker.compose.service", "mysql")):
        if labels.get(label) != expected:
            problems.append(f"drill 容器标签 {label} 不匹配")
    for label, expected in (("com.docker.compose.project", main_project), ("com.docker.compose.service", "mysql")):
        if main_labels.get(label) != expected:
            problems.append(f"主 MySQL 容器标签 {label} 不匹配，无法确认生产基准")
    if not container.get("Id") or not main.get("Id") or container.get("Id") == main.get("Id"):
        problems.append("drill 与主 MySQL 必须是不同的有效容器")
    if not (container.get("State") or {}).get("Running"):
        problems.append("drill MySQL 容器未运行")

    host = container.get("HostConfig") or {}
    if host.get("NetworkMode") in ("host", "none") or str(host.get("NetworkMode", "")).startswith("container:"):
        problems.append("drill 禁止 host/none/container 网络")
    if host.get("PublishAllPorts") or any((host.get("PortBindings") or {}).values()):
        problems.append("drill 存在端口发布配置")
    if any(((container.get("NetworkSettings") or {}).get("Ports") or {}).values()):
        problems.append("drill 存在实际发布端口")
    # HostConfig.Binds 也可能以旧格式列出正常命名卷，实际类型由 Mounts 验证。
    if host.get("VolumesFrom") or host.get("Tmpfs") or host.get("Privileged") or host.get("Devices"):
        problems.append("drill 含 volumes_from/tmpfs/privileged/devices，禁止导入")

    mounts = container.get("Mounts") or []
    drill_data = _datadir_mounts(container)
    main_data = _datadir_mounts(main)
    if len(mounts) != 1 or len(drill_data) != 1:
        problems.append("drill 必须仅有一个 /var/lib/mysql 挂载")
        return problems
    if len(main_data) != 1:
        problems.append("主 MySQL 必须存在唯一 /var/lib/mysql 挂载供比对")
        return problems
    mount = drill_data[0]
    source_key = config["services"]["mysql"]["volumes"][0]["source"]
    expected_name = config["volumes"][source_key]["name"]
    if mount.get("Type") != "volume" or mount.get("RW") is not True:
        problems.append("drill 数据目录必须是可写命名卷")
    if mount.get("Name") != expected_name or volume.get("Name") != expected_name:
        problems.append("drill 实际数据卷名与已验证 Compose 配置不一致")
    if volume.get("Driver") != "local" or mount.get("Driver") != "local":
        problems.append("drill 数据卷必须使用 local driver")
    if volume.get("Options"):
        problems.append("drill 实际数据卷含 Options，可能映射主库路径")
    if volume.get("Scope") != "local":
        problems.append("drill 数据卷 Scope 必须是 local")
    volume_labels = volume.get("Labels") or {}
    if volume_labels.get("com.docker.compose.project") != project or volume_labels.get("com.docker.compose.volume") != source_key:
        problems.append("drill 数据卷 Compose 项目/卷标签不匹配")

    source = mount.get("Source")
    mountpoint = volume.get("Mountpoint")
    main_source = main_data[0].get("Source")
    if not all(isinstance(p, str) and p.startswith("/") for p in (source, mountpoint, main_source)):
        problems.append("缺少有效的 drill/主库实际 Source 或卷 Mountpoint")
    else:
        source, mountpoint, main_source = map(posixpath.normpath, (source, mountpoint, main_source))
        if source != mountpoint:
            problems.append("drill Mount.Source 与 volume Mountpoint 不一致")
        # 同一路径以及父子路径均不能作为两个隔离数据目录。
        if posixpath.commonpath((source, main_source)) in (source, main_source):
            problems.append("drill 数据目录与主 MySQL 相同或包含，禁止导入/清理")
    if mount.get("Name") == main_data[0].get("Name"):
        problems.append("drill 数据卷与主 MySQL 数据卷相同")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "project", "main-project", "container", "volume", "main-container"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_object(args.config)
        container = load_object(args.container)
        volume = load_object(args.volume)
        main_container = load_object(args.main_container)
        problems = check_runtime(config, args.project, args.main_project, container, volume, main_container)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(f"[drill-runtime] 输入错误：{exc}", file=sys.stderr)
        return 2
    if problems:
        for problem in problems:
            print(f"[drill-runtime] 拒绝：{problem}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "project": args.project, "container_id": container["Id"], "data_volume": volume["Name"], "data_source": volume["Mountpoint"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
