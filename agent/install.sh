#!/usr/bin/env bash
# =============================================================================
# ERP 租户服务器 Agent 2 安装脚本（Ubuntu 24.04 / systemd）
#
# 用法：
#   sudo bash install.sh https://erp.example.com <一次性接入令牌> <实例ID>
#
# 安装后的布局：
#   /opt/erp-agent/versions/<版本>/   每个 Agent 版本独立目录（自升级只切 current 软链）
#   /opt/erp-agent/current -> versions/<版本>
#   /etc/erp-agent/config.json        节点 UUID / 节点令牌 / 部署路径（升级不修改）
#   /var/lib/erp-agent/               本地 SQLite 执行日志、指标缓存
#
# 两个 systemd unit（采集与执行分离、权限收敛）：
#   erp-agent.service           采集进程：指标 + 30s 心跳 + 缓存/结果补报（只读）
#   erp-agent-executor.service  执行进程：任务租约/续租/执行（可写部署目录）
# =============================================================================
set -euo pipefail

CENTER="${1:?用法: install.sh <center_url> <enroll_token> <instance_id>}"
TOKEN="${2:?缺少一次性接入令牌}"
INSTANCE="${3:-0}"
BASE_DIR="/opt/erp-agent"
CONFIG_DIR="/etc/erp-agent"
CONFIG="${CONFIG_DIR}/config.json"
STATE_DIR="/var/lib/erp-agent"
COLLECTOR_USER="erp-agent"
EXECUTOR_USER="erp-agent-exec"
SHARED_GROUP="erp-agent"
COLLECTOR_UNIT="/etc/systemd/system/erp-agent.service"
EXECUTOR_UNIT="/etc/systemd/system/erp-agent-executor.service"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 或 sudo 执行" >&2
  exit 1
fi
python3 --version >/dev/null 2>&1 || { echo "需要 python3" >&2; exit 1; }

AGENT_VERSION="$(python3 - "$SRC_DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from agentlib.constants import AGENT_VERSION
print(AGENT_VERSION)
PY
)"
echo "[install] Agent 版本：${AGENT_VERSION}"

# ------------------------------------------------------------------ 用户与目录
# 采集进程：普通系统用户，只读部署目录；执行进程：独立用户，可写部署目录。
if ! getent group "$SHARED_GROUP" >/dev/null 2>&1; then
  groupadd --system "$SHARED_GROUP"
fi
for user in "$COLLECTOR_USER" "$EXECUTOR_USER"; do
  if ! id -u "$user" >/dev/null 2>&1; then
    useradd --system --home-dir "$BASE_DIR" --shell /usr/sbin/nologin "$user"
  fi
  usermod -aG "$SHARED_GROUP" "$user"
  if getent group docker >/dev/null 2>&1; then
    # 读取容器状态 / 执行 compose 需要 docker 组；docker socket 本身等价 root，
    # 因此 unit 另加 NoNewPrivileges/ProtectSystem 等收敛（见 README「安全边界」）。
    usermod -aG docker "$user"
  fi
done

install -d -m 755 "$BASE_DIR" "$BASE_DIR/versions"
install -d -m 750 -o root -g "$SHARED_GROUP" "$CONFIG_DIR"
install -d -m 2770 -o "$COLLECTOR_USER" -g "$SHARED_GROUP" "$STATE_DIR"

# ------------------------------------------------------------------ 版本目录
VERSION_DIR="$BASE_DIR/versions/$AGENT_VERSION"
rm -rf "$VERSION_DIR"
install -d -m 755 "$VERSION_DIR"
install -m 755 "$SRC_DIR/erp_agent.py" "$VERSION_DIR/erp_agent.py"
install -m 755 "$SRC_DIR/executor.py" "$VERSION_DIR/executor.py"
cp -a "$SRC_DIR/agentlib" "$VERSION_DIR/agentlib"
for doc in README.md PROTOCOL.md; do
  if [ -f "$SRC_DIR/$doc" ]; then
    install -m 644 "$SRC_DIR/$doc" "$VERSION_DIR/$doc"
  fi
done
python3 - "$VERSION_DIR" "$AGENT_VERSION" <<'PY'
import json, sys
from pathlib import Path
from datetime import datetime, timezone
target = Path(sys.argv[1])
meta = {
    "version": sys.argv[2],
    "commit": "",
    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "source": "install.sh",
}
(target / ".agent-self.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
ln -sfn "$VERSION_DIR" "$BASE_DIR/current"
chown -R root:root "$BASE_DIR/versions"

# ------------------------------------------------------------------ 注册（写入节点身份）
if [ ! -f "$CONFIG" ]; then
  python3 "$BASE_DIR/current/erp_agent.py" enroll \
    --center "$CENTER" \
    --token "$TOKEN" \
    --instance "$INSTANCE" \
    --config "$CONFIG"
fi
chown root:"$SHARED_GROUP" "$CONFIG"
chmod 640 "$CONFIG"
chown -R "$COLLECTOR_USER":"$SHARED_GROUP" "$STATE_DIR"
install -d -m 700 -o "$EXECUTOR_USER" -g "$SHARED_GROUP" "$STATE_DIR/.docker"
chmod 2770 "$STATE_DIR"
chmod 660 "$STATE_DIR"/*.db "$STATE_DIR"/*.db-* 2>/dev/null || true

# ------------------------------------------------------------------ systemd
# 旧版单 unit 安装先停掉，避免两个采集进程同时上报。
systemctl stop erp-agent.service >/dev/null 2>&1 || true

cat > "$COLLECTOR_UNIT" <<'UNIT'
[Unit]
Description=ERP tenant server agent collector (telemetry + heartbeat + replay)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/erp-agent/current/erp_agent.py run --config /etc/erp-agent/config.json
Restart=always
RestartSec=5
User=erp-agent
Group=erp-agent
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/erp-agent
ReadOnlyPaths=/opt/erp-agent /opt/ventasync /etc/erp-agent
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

cat > "$EXECUTOR_UNIT" <<'UNIT'
[Unit]
Description=ERP tenant server agent executor (leased jobs + local journal)
After=network-online.target docker.service erp-agent.service
Wants=network-online.target

[Service]
Type=simple
Environment=HOME=/var/lib/erp-agent
Environment=DOCKER_CONFIG=/var/lib/erp-agent/.docker
ExecStart=/usr/bin/python3 /opt/erp-agent/current/erp_agent.py executor --config /etc/erp-agent/config.json
Restart=always
RestartSec=5
User=erp-agent-exec
Group=erp-agent
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=/var/lib/erp-agent /opt/ventasync /tmp
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now erp-agent.service erp-agent-executor.service
sleep 2
systemctl --no-pager --full status erp-agent.service | head -12 || true
echo
systemctl --no-pager --full status erp-agent-executor.service | head -12 || true
cat <<EOF

Agent 已安装：
  采集进程：systemctl status erp-agent
  执行进程：systemctl status erp-agent-executor
  日志：    journalctl -u erp-agent -f / journalctl -u erp-agent-executor -f
  状态：    python3 /opt/erp-agent/current/erp_agent.py status
  自升级：  python3 /opt/erp-agent/current/erp_agent.py self-upgrade --source <新版本源码目录> --version <版本>
  自测：    python3 /opt/erp-agent/current/erp_agent.py selftest
EOF
