# ERP 管理中心独立 watchdog

用于在**集成网关服务器或独立小主机**上探测管理中心，不依赖中心数据库和中心邮件配置。

## 能力

- 每 60 秒请求 `GET /healthz`
- 连续 3 次失败发送告警邮件
- 恢复后发送恢复邮件
- 状态保存在本地文件，重启不重复告警
- 独立 SMTP 配置，不读取中心 `email_profiles`

## 部署

```bash
sudo useradd --system --home /opt/erp-watchdog --shell /usr/sbin/nologin erp-watchdog || true
sudo install -d -o erp-watchdog -g erp-watchdog /opt/erp-watchdog /var/lib/erp-watchdog
sudo install -m 755 erp_watchdog.py /opt/erp-watchdog/erp_watchdog.py
sudo install -m 644 erp-watchdog.service /etc/systemd/system/erp-watchdog.service
sudo tee /etc/erp-watchdog.env >/dev/null <<'EOF'
ERP_WATCHDOG_TARGET=https://erp.example.com
ERP_WATCHDOG_INTERVAL=60
ERP_WATCHDOG_FAIL_THRESHOLD=3
ERP_WATCHDOG_STATE=/var/lib/erp-watchdog/state.json
ERP_WATCHDOG_SMTP_HOST=smtp.example.com
ERP_WATCHDOG_SMTP_PORT=465
ERP_WATCHDOG_SMTP_USER=alert@example.com
ERP_WATCHDOG_SMTP_PASSWORD=change-me
ERP_WATCHDOG_SMTP_FROM=alert@example.com
ERP_WATCHDOG_SMTP_TO=ops@example.com
EOF
sudo chmod 600 /etc/erp-watchdog.env
sudo systemctl daemon-reload
sudo systemctl enable --now erp-watchdog
```

## 测试

```bash
ERP_WATCHDOG_TARGET=https://erp.alienyan.store ERP_WATCHDOG_DRY_RUN=1 \
  python3 erp_watchdog.py --once
```

输出包含 `ok`、HTTP 状态、耗时与本地状态。`ERP_WATCHDOG_DRY_RUN=1` 只打印邮件，不真正发送。

建议把该服务部署在管理中心之外的另一台服务器；与管理中心同机部署只能覆盖部分故障场景。
