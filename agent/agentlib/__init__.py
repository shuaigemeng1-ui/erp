"""Agent 2 共享库（仅标准库）。

模块划分：

* ``constants``：版本、协议、时间参数、状态与能力常量；
* ``config``：``/etc/erp-agent/config.json`` 读写（升级不修改该文件）；
* ``httpc``：管理中心 HTTP 客户端（可注入 transport，错误分类，404 降级）；
* ``journal``：本地 SQLite 执行日志（租约、阶段、子进程、待发送结果）；
* ``telemetry``：宿主机/Docker 指标采集与断网缓存补报；
* ``heartbeat``：Agent 心跳与任务租约续租；
* ``jobs``：任务执行协议（单实例单变更任务、unknown 占用、结果先落库后上报）；
* ``versions``：应用升级的本地 git 版本目录构建（完整 commit 校验）；
* ``upgrade``：Agent 自身升级 helper（版本目录切换、systemd 重启验证、回滚）；
* ``selftest``：不依赖真实服务器的离线自测。
"""

from __future__ import annotations

from .constants import AGENT_VERSION, PROTOCOL_VERSION

__all__ = ["AGENT_VERSION", "PROTOCOL_VERSION"]
