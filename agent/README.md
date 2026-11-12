# ERP 租户服务器 Agent

租户服务器安装后，每 30 秒向统一管理中心上报：

- 宿主机 CPU、负载、内存、Swap、磁盘、inode、网络收发速率
- 内网 IP、观测出口 IP、主机名、系统、CPU 核数、总内存、总磁盘
- Docker 容器状态与健康状态
- Agent 版本、应用版本

管理中心按 `node_uuid` 绑定实例，IP 变化不会产生新节点。断网时本地缓存最多
2880 条（24 小时），恢复后按原采样时间补报；管理中心以 `sampled_at` 与
`received_at` 分开存储，历史补报不会伪造实时在线状态。

## 安装

1. 平台后台「服务器与实例」创建实例，生成一次性接入令牌（30 分钟有效）。
2. 在租户服务器执行：

```bash
sudo bash install.sh https://erp.alienyan.store <enroll_token> <instance_id>
```

3. 查看状态：

```bash
systemctl status erp-agent
journalctl -u erp-agent -f
```

## 手动运行

```bash
python3 erp_agent.py enroll --center https://erp.example.com --token <token> --config ./agent.json
python3 erp_agent.py run --config ./agent.json --once
```

Agent 只读采集宿主机与 Docker 状态，不执行任意 Shell、不接收远程命令；后续受限运维执行器
再单独接入。
