#!/usr/bin/env bash
# 新独立租户服务器初始化模板（批次 4）。
#
# 用法（默认 --plan，只打印不改系统）：
#   bash install.sh --env-file /opt/ventasync/deploy/.env
#   sudo bash install.sh --env-file /opt/ventasync/deploy/.env --apply \
#        --confirm INSTALL-<TENANT_ID>-<INSTANCE_ID> \
#        [--install-agent --center-url https://center.example.com --agent-enroll-token <一次性令牌>]
#
# 本脚本只做“服务器就绪”类操作：目录/权限、备份目录、可选 Agent2 安装、检查清单输出；
# 不启动/不重启业务容器，不修改中心库；SSH 指纹、证书、域名切换仍需按清单人工确认。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/_common.sh"

usage() {
  cat <<'EOF'
用法：
  install.sh --env-file <file> [--plan]
  sudo install.sh --env-file <file> --apply --confirm INSTALL-<TENANT_ID>-<INSTANCE_ID> \
       [--repo-dir /opt/ventasync] [--install-agent --center-url <url> --agent-enroll-token <token>]

默认只打印计划；--apply 仅执行：目录/权限检查、install_backup_dirs.sh、可选 Agent2 安装。
EOF
}

ENV_FILE="${DEPLOY_DIR}/.env"
REPO_DIR="/opt/ventasync"
APPLY=0
CONFIRM=""
INSTALL_AGENT=0
CENTER_URL=""
ENROLL_TOKEN=""

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --repo-dir) REPO_DIR="${2:-}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --confirm) CONFIRM="${2:-}"; shift 2 ;;
    --install-agent) INSTALL_AGENT=1; shift ;;
    --center-url) CENTER_URL="${2:-}"; shift 2 ;;
    --agent-enroll-token) ENROLL_TOKEN="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

usage_preamble
require_env "$ENV_FILE" TENANT_ID INSTANCE_ID DEPLOYMENT_MODE APP_ORIGIN
TENANT_ID="$(env_get "$ENV_FILE" TENANT_ID)"
INSTANCE_ID="$(env_get "$ENV_FILE" INSTANCE_ID)"
DOMAIN="$(env_get "$ENV_FILE" TRUSTED_HOSTS)"

if [ "$APPLY" -eq 1 ]; then
  [ "$(id -u)" -eq 0 ] || die "--apply 需要 root（sudo）"
  confirm_or_die "INSTALL-${TENANT_ID}-${INSTANCE_ID}" "$CONFIRM"
  require_cmd docker curl
  docker compose version >/dev/null 2>&1 || die "docker compose v2 不可用"
  [ -d "${REPO_DIR}/deploy" ] || die "仓库目录不存在：${REPO_DIR}/deploy（先 git clone/发布）"
  [ -f "${REPO_DIR}/deploy/docker-compose.yml" ] || die "缺少 ${REPO_DIR}/deploy/docker-compose.yml"
  bash "${SCRIPT_DIR}/config.sh" --check --env-file "$ENV_FILE"
else
  warn "当前为 plan 模式：只打印，不修改系统；执行加 --apply + --confirm"
fi

log "实例：tenant_id=${TENANT_ID} instance_id=${INSTANCE_ID} project=$(env_get "$ENV_FILE" COMPOSE_PROJECT_NAME)"
log "域名：$(env_get "$ENV_FILE" APP_ORIGIN)（TRUSTED_HOSTS=${DOMAIN:-<空>}）"

# 1) 备份目录/共享组/密钥目录（root，幂等）
log "[1/4] 备份目录初始化：sudo bash deploy/scripts/install_backup_dirs.sh"
run_or_echo bash "${SCRIPT_DIR}/../install_backup_dirs.sh"

# 2) Agent2 安装（可选；使用一次性接入令牌，不回显）
if [ "$INSTALL_AGENT" -eq 1 ]; then
  [ -n "$CENTER_URL" ] && [ -n "$ENROLL_TOKEN" ] || die "--install-agent 需要 --center-url 与 --agent-enroll-token"
  [ -f "${REPO_DIR}/agent/install.sh" ] || die "缺少 ${REPO_DIR}/agent/install.sh（发布包需包含 agent/）"
  log "[2/4] 安装 Agent2：${CENTER_URL} instance=${INSTANCE_ID}"
  if [ "$APPLY" -eq 1 ]; then
    bash "${REPO_DIR}/agent/install.sh" "$CENTER_URL" "$ENROLL_TOKEN" "$INSTANCE_ID"
  else
    echo "[plan] bash ${REPO_DIR}/agent/install.sh ${CENTER_URL} <token> ${INSTANCE_ID}"
  fi
else
  log "[2/4] 跳过 Agent2 安装（未指定 --install-agent）；后续可手工执行 agent/install.sh"
fi

# 3) 域名/证书（只检查/打印，不自动改 Caddy）
log "[3/4] 证书/域名检查"
if command -v caddy >/dev/null 2>&1; then
  caddy version >/dev/null 2>&1 && log "Caddy 已安装；请将 ${DOMAIN} 反代到 127.0.0.1:$(env_get "$ENV_FILE" HTTP_PORT)" \
    || warn "Caddy 命令存在但不可用"
else
  warn "未检测到 Caddy；请用宿主机 Nginx/Caddy 为 ${DOMAIN} 配置 TLS 并反代到 127.0.0.1:$(env_get "$ENV_FILE" HTTP_PORT)"
fi
[ -f "${REPO_DIR}/deploy/caddy/Caddyfile.example" ] && log "Caddyfile 示例：${REPO_DIR}/deploy/caddy/Caddyfile.example"

# 4) SSH 指纹/节点接入（人工确认，脚本不自动信任）
log "[4/4] SSH 指纹与节点接入（人工执行，禁止跳过核对）"
cat <<EOF
  1) 平台（中心）为该实例/节点保存 SSH 账号：平台 → 服务器节点 → SSH 配置；
  2) ssh_host 必须填写本机公网/可达地址，禁止回退 Agent 上报 IP；
  3) 首次 SSH exec 成功后记录 SHA256 指纹；再次查询应为 fingerprint_status=verified；
  4) 如更换主机/重装系统，先在平台“重置主机指纹”并人工核对新指纹；
  5) 备份密钥 /etc/ventasync/backup.key 需异地妥善保存（丢失无法解密异地备份）。
EOF

if [ "$APPLY" -eq 1 ]; then
  log "安装准备完成。下一步：config.sh --check → migrate.sh --plan → 停写对账 → 启动服务 → verify_isolation.sh"
else
  log "plan 完成；确认无误后加 --apply --confirm INSTALL-${TENANT_ID}-${INSTANCE_ID} 执行。"
fi
