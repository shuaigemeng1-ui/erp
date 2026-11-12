# 服务器与实例监控（阶段一）

统一管理中心的「服务器与实例」页面用于登记租户独立实例、接入服务器 Agent、查看资源指标、执行公网探测和查看告警。

## 架构

```text
租户服务器（Agent，每 30 秒）
   │  HTTPS 指标上报 / 一次性接入令牌
   ▼
统一 ERP 管理中心（FastAPI + 中心 MySQL）
   │  服务器资产、指标样本、探测结果、告警
   ▼
平台管理员「服务器与实例」页面
```

管理中心同时由调度进程定时探测已登记实例的 `https://<域名>/` 与 `/healthz`，检查用户实际访问链路。公网探测与管理中心自身健康检查分离。

## 数据表

全部在中心库，无外键，表/字段带中文注释：

| 表 | 用途 |
|---|---|
| `tenant_instances` | 租户独立实例、域名、环境、状态、版本 |
| `server_nodes` | 节点固定 UUID、主机资产、内网 IP/配置公网 IP/观测出口 IP、Agent 版本、最后心跳、令牌摘要 |
| `node_metric_samples` | 每 30 秒原始指标，默认保留 7 天 |
| `node_checks` | 公网 root/healthz 探测结果 |
| `alert_events` | 告警事件，同节点同类型未恢复告警合并，恢复后自动关闭 |

## 接入流程

1. 平台管理员在「服务器与实例」创建实例，填写租户、名称、域名。
2. 点击「生成接入令牌」，获得一次性 token（30 分钟有效）和安装命令。
3. 在租户服务器执行：

```bash
sudo bash agent/install.sh https://erp.alienyan.store <enroll_token> <instance_id>
```

4. Agent 注册成功后写入 `/opt/erp-agent/config.json`，由 systemd 常驻。
5. 首次指标上报后实例自动变为 `active`，节点显示 `online`。

Agent 使用标准库采集，不依赖 psutil；只读采集宿主机和 Docker 状态，不执行远程 Shell。

## 监控指标

- CPU 使用率、1/5/15 分钟负载
- 内存总量/已用/可用/使用率、Swap
- 各磁盘总量/已用/可用/使用率/inode
- 网络累计字节与每秒收发速率
- Docker 容器状态、健康状态
- 内网 IP、Agent 观测出口 IP、主机名、系统、CPU 核数、内存/磁盘总量
- Agent 版本、应用版本（`ERP_APP_VERSION` 环境变量）

内存使用率按 `MemAvailable` 计算；补报样本同时保存 `sampled_at` 与 `received_at`，历史补报不会伪造实时在线。

## 默认告警

| 类型 | 阈值 |
|---|---|
| Agent 失联 | 超过 90 秒未上报 |
| 公网不可访问 | 任一探测失败 |
| CPU 过高 | ≥ 85% |
| 内存过高 | ≥ 90% |
| 磁盘预警/严重 | ≥ 85% / ≥ 95% |

当前版本采用即时阈值；告警通过系统配置中的默认邮件账号发送给平台管理员，同一告警 30 分钟内不重复提醒；CPU/内存持续窗口和按实例覆盖阈值在后续阶段补齐。告警在页面「告警中心」可确认。原始指标和探测记录默认保留 7 天，由调度进程每天清理一次。

## 主要接口

| 接口 | 说明 |
|---|---|
| `POST /api/v1/agent/enroll` | 一次性令牌注册节点 |
| `POST /api/v1/agent/telemetry` | 节点 Bearer 令牌上报指标 |
| `GET /api/v1/platform/instances` | 实例列表 |
| `POST /api/v1/platform/instances` | 登记实例 |
| `GET/PATCH /api/v1/platform/instances/{id}` | 实例详情/更新 |
| `GET/POST /api/v1/platform/instances/{id}/nodes` | 节点列表/生成接入令牌 |
| `POST /api/v1/platform/nodes/{id}/probe` | 立即公网探测 |
| `GET /api/v1/platform/nodes/{id}/metrics` | 指标趋势 |
| `GET /api/v1/platform/alerts` | 告警列表 |
| `POST /api/v1/platform/alerts/{id}/ack` | 确认告警 |

Agent 接口不依赖浏览器 Cookie，使用节点 Bearer 令牌认证；平台接口仅平台管理员可访问。

## 运维与排查

```bash
systemctl status erp-agent
journalctl -u erp-agent -f
cat /opt/erp-agent/config.json
```

管理中心：

```bash
cd /opt/ventasync/deploy
docker compose logs -f scheduler
```

阶段一已验证当前服务器（实例 1 / 节点 1）在线并持续上报 CPU、内存、磁盘与容器状态。
