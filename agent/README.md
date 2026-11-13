# ERP 租户服务器 Agent（Agent 2）

租户服务器安装后由两个 systemd 进程组成，职责与权限分离：

| 进程 | unit | 职责 |
| --- | --- | --- |
| 采集 | `erp-agent.service` | 每 30s 采集宿主机/容器指标、独立心跳、断网缓存补报、待发送结果补报（只读） |
| 执行 | `erp-agent-executor.service` | 轮询运维任务、120s 租约 + 30s 续租、受限执行、本地 SQLite 执行日志、结果先落库后上报 |

管理中心按 `node_uuid` 绑定实例，IP 变化不产生新节点；断网时指标最多缓存 2880 条（24h），
恢复后按原采样时间补报（`boot_id + sample_seq` 幂等去重），不伪造实时在线状态。

## 安装

1. 平台后台「服务器与实例」创建实例，生成一次性接入令牌（30 分钟有效）。
2. 在租户服务器执行：

```bash
sudo bash install.sh https://erp.alienyan.store <enroll_token> <instance_id>
```

3. 查看状态：

```bash
systemctl status erp-agent erp-agent-executor
journalctl -u erp-agent -f            # 采集/心跳/补报
journalctl -u erp-agent-executor -f   # 任务执行
python3 /opt/erp-agent/current/erp_agent.py status
```

## 目录与文件

```text
/opt/erp-agent/versions/<版本>/   Agent 代码（每个版本独立目录，自升级只切换软链）
/opt/erp-agent/current            当前版本软链
/etc/erp-agent/config.json        节点身份与部署路径（0600/0640，升级不会修改）
/var/lib/erp-agent/erp-agent.db   本地 SQLite 执行日志（租约/阶段/子进程/待发送结果）
/var/lib/erp-agent/telemetry-cache.json  断网指标缓存
```

## 手动运行

```bash
python3 erp_agent.py enroll --center https://erp.example.com --token <token> --config ./agent.json
python3 erp_agent.py run --config ./agent.json --once        # 采集一次
python3 erp_agent.py run --config ./agent.json               # 采集进程（systemd 调用）
python3 erp_agent.py executor --config ./agent.json          # 执行进程
python3 erp_agent.py heartbeat --config ./agent.json         # 发送一次心跳（排障）
python3 erp_agent.py status  --config ./agent.json           # 本地租约/待补报概览
python3 erp_agent.py reconcile --job-id 12 --note "人工确认" # 解除 unknown 占用
```

## 离线自测

不依赖真实服务器、网络、docker、systemd：

```bash
python3 erp_agent.py selftest          # 内置自测（journal/租约/补报/commit 校验/自升级回滚）
python -m pytest agent/tests -q        # 仓库内单元测试（21 项）
```

测试覆盖：SQLite 执行日志与终态幂等、单实例单任务、unknown 占用与 reconcile、
心跳/续租/409 处理、事件序号单调与重放、结果补报与退避、批量指标降级、
完整 commit 校验、版本目录构建（无 `git reset --hard`）、Agent 自升级与回滚。

## 升级

应用升级（`deploy/upgrade` 任务）：

* `upgrade` 必须携带完整 40/64 位 commit，Agent 在本地 `git worktree` 独立版本目录内构建，
  **不再原地 `git reset --hard`**；构建 + 服务 readiness 通过后才切换当前版本指针；
* `restart/backup/logs/deploy` 行为与旧版一致（`deploy` 只启动当前版本，不做 git 操作）。

Agent 自身升级：

```bash
python3 /opt/erp-agent/current/erp_agent.py self-upgrade \
  --source /opt/ventasync/agent --version 2.1.0 --commit <完整commit>
```

独立版本目录 + `current` 软链原子切换 + 保留节点 UUID/config + systemd 重启验证，
失败自动回滚到上一版本。旧协议 Agent 不上报 `protocol_version=2`，中心不会给它派发变更任务。

## 任务可靠性

* 同一实例同时只允许一个变更任务（本地租约唯一 + 单线程执行）；
* 租约 120s、每 30s 续租；续租 409 或连续失败 3 次 → 结果 unknown；
* unknown 继续占用实例，**reconcile 之前不领取新任务、不自动重试**；
* 结果先写 SQLite 再上报，网络恢复后补发，终态幂等；409 等永久错误落死信停止重试；
* 执行器重启时把未完成租约修正为 unknown 并调用服务端 reconcile 核对。

## 安全边界

* 只执行白名单任务类型（restart/backup/logs/deploy/upgrade），命令使用参数列表，禁止任意 Shell；
* 所有命令输出做密钥脱敏，日志与本地记录不保存明文凭据；
* 执行进程与采集进程分离、不同用户；采集进程只读部署目录（`ProtectSystem=strict`），
  执行进程才可写 `/opt/ventasync`；
* 已知限制：读取容器状态与执行 compose 需要 docker 组，docker socket 本身等价 root；
  因此两个 unit 均启用 `NoNewPrivileges`、`ProtectHome`、`PrivateTmp` 收敛影响面，
  生产环境建议进一步使用 rootless docker 或独立 socket proxy。
