#!/usr/bin/env bash
# ERP 租户服务器 Agent 安装脚本（Ubuntu 24.04 / systemd）
# 用法：
#   sudo bash install.sh https://erp.example.com <一次性接入令牌> <实例ID>
set -euo pipefail

CENTER="${1:?用法: install.sh <center_url> <enroll_token> <instance_id>}"
TOKEN="${2:?缺少一次性接入令牌}"
INSTANCE="${3:-0}"
BASE_DIR="/opt/erp-agent"
CONFIG="${BASE_DIR}/config.json"
SERVICE="/etc/systemd/system/erp-agent.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 或 sudo 执行" >&2
  exit 1
fi

python3 --version >/dev/null 2>&1 || { echo "需要 python3" >&2; exit 1; }
install -d -m 755 "$BASE_DIR"
install -m 755 "$(dirname "$0")/erp_agent.py" "$BASE_DIR/erp_agent.py"

python3 "$BASE_DIR/erp_agent.py" enroll \
  --center "$CENTER" \
  --token "$TOKEN" \
  --instance "$INSTANCE" \
  --config "$CONFIG"

cat > "$SERVICE" <<'UNIT'
[Unit]
Description=ERP tenant server monitoring agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/erp-agent/erp_agent.py run --config /opt/erp-agent/config.json
Restart=always
RestartSec=5
User=root
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now erp-agent.service
sleep 2
systemctl --no-pager --full status erp-agent.service | head -20
echo "Agent 已安装并启动。日志：journalctl -u erp-agent -f"
