# ERP 管理中心独立 watchdog（中心外）

管理中心内部告警邮件依赖中心数据库、Redis 与邮件配置；中心整体不可用（网关、容器、数据库、Redis）
时这条链路会一起失效。本 watchdog 部署在**管理中心之外**的服务器（建议集成网关或独立小主机），
使用**独立 SMTP 配置**探测管理中心并发送告警 / 恢复邮件。

## 能力

- 多端点探测：默认 `GET /healthz` + `GET /api/v1/runtime`，任一失败按整体失败计；
- 连续 N 次失败（默认 3）才告警；未恢复期间按冷却时间（默认 30 分钟）重复提醒；
- 恢复后发送恢复邮件；可在配置中关闭；
- 可选延迟告警：探活成功但响应超过阈值时提醒慢响应（默认关闭）；
- 状态写入本地 JSON（原子写、600 权限），重启不重复告警；
- 独立 SMTP：`ssl`(465) / `starttls`(587) / `none`(25，仅内网)；
- 只依赖 Python 标准库，不访问中心数据库/Redis/邮件配置。

## 与中心内告警的分工

| 场景 | 负责方 |
|---|---|
| CPU/内存/磁盘/公网探测/备份等业务告警 | 管理中心调度进程（`ops.service.run_center_maintenance`） |
| 管理中心整体不可用 / 恢复 | **本 watchdog（独立 SMTP）** |
| 告警列表的维护窗口/抑制/恢复字段 | 管理中心 `/platform/alerts`（task-30 补齐） |

两者互不依赖：中心可用时由中心发业务告警；中心不可用时 watchdog 独立发「管理中心不可访问」。

## 部署

```bash
sudo useradd --system --home /opt/erp-watchdog --shell /usr/sbin/nologin erp-watchdog || true
sudo install -d -o erp-watchdog -g erp-watchdog /opt/erp-watchdog /var/lib/erp-watchdog
sudo install -m 755 deploy/watchdog/erp_watchdog.py /opt/erp-watchdog/erp_watchdog.py
sudo install -m 644 deploy/watchdog/erp-watchdog.service /etc/systemd/system/erp-watchdog.service
sudo install -m 600 deploy/watchdog/erp-watchdog.env.example /etc/erp-watchdog.env
sudo editor /etc/erp-watchdog.env   # 填写 target / SMTP
sudo systemctl daemon-reload
sudo systemctl enable --now erp-watchdog
```

## 配置与自检

配置项见 `erp-watchdog.env.example`，全部使用 `ERP_WATCHDOG_*` 环境变量：

```bash
# 配置自检（不发网络请求、不发信）
sudo -u erp-watchdog env $(cat /etc/erp-watchdog.env | grep -v '^#') \
  python3 /opt/erp-watchdog/erp_watchdog.py --self-test

# 单次演练（只打印邮件，不发送）
sudo -u erp-watchdog env $(cat /etc/erp-watchdog.env | grep -v '^#') ERP_WATCHDOG_DRY_RUN=1 \
  python3 /opt/erp-watchdog/erp_watchdog.py --once
```

输出为 JSON 行（systemd journal 可直接检索）：`watchdog_start`、`watchdog_cycle`、`mail_sent`、
`mail_failed`、`watchdog_stop`。`watchdog_cycle.actions` 会显示本轮触发的
`alert` / `recovery` / `latency_warn` / `latency_ok`。

## 故障排查

```bash
systemctl status erp-watchdog
journalctl -u erp-watchdog -f
cat /var/lib/erp-watchdog/state.json
```

- `watchdog_cycle.ok=false`：管理中心入口或端点不可用，检查网关/容器/数据库；
- `mail_skipped reason=smtp_not_configured`：SMTP 配置缺失；
- `mail_failed`：SMTP 认证/网络问题（注意 465 用 ssl、587 用 starttls）；
- 状态文件损坏会自动重建，但会丢失「不重复告警」记忆，建议放在持久目录。

> 旧版脚本位于仓库根目录 `watchdog/`（保留兼容）。新部署请使用本目录版本。
