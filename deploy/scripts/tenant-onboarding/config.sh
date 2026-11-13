#!/usr/bin/env bash
# 第二租户实例配置模板渲染/校验（批次 4）。
#
# 用法：
#   bash config.sh --check  --env-file /opt/ventasync/deploy/.env
#   bash config.sh --render --tenant-id 3 --instance-id 2 --domain zuhu2.example.com \
#        --project ventasync-tenant-3 --image-tag <commit> --output /opt/ventasync/deploy/.env
#
# 安全：
#   * 默认只 --check；--render 生成新文件，绝不覆盖已有文件（除非 --force，且先备份）；
#   * 生成文件权限 600；密码随机生成，不打印到终端/日志；
#   * 不连接数据库、不修改运行中服务。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/_common.sh"

usage() {
  cat <<'EOF'
用法：
  config.sh --check  --env-file <file>
  config.sh --render --tenant-id <id> --instance-id <id> --domain <host> \
            --project <compose-project> --image-tag <tag> --output <file> \
            [--gateway-url <url>] [--mysql-root-password <pwd>] [--db-password <pwd>] [--force]

说明：
  --render 会从 tenant.env.example 生成新租户 .env（不覆盖既有文件），并自动校验绑定一致性。
EOF
}

MODE=""
ENV_FILE="${DEPLOY_DIR}/.env"
OUTPUT=""
TENANT_ID=""
INSTANCE_ID=""
DOMAIN=""
PROJECT=""
IMAGE_TAG=""
GATEWAY_URL=""
MYSQL_ROOT_PASSWORD=""
DB_PASSWORD=""
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE="check"; shift ;;
    --render) MODE="render"; shift ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --output) OUTPUT="${2:-}"; shift 2 ;;
    --tenant-id) TENANT_ID="${2:-}"; shift 2 ;;
    --instance-id) INSTANCE_ID="${2:-}"; shift 2 ;;
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --project) PROJECT="${2:-}"; shift 2 ;;
    --image-tag) IMAGE_TAG="${2:-}"; shift 2 ;;
    --gateway-url) GATEWAY_URL="${2:-}"; shift 2 ;;
    --mysql-root-password) MYSQL_ROOT_PASSWORD="${2:-}"; shift 2 ;;
    --db-password) DB_PASSWORD="${2:-}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

is_positive_int() { [[ "${1:-}" =~ ^[1-9][0-9]*$ ]]; }

validate_env() {
  local file="$1" problems=0
  [ -f "$file" ] || die "环境文件不存在：$file"
  local checks=(
    "DEPLOYMENT_MODE|tenant"
  )
  local key expected actual
  for item in "${checks[@]}"; do
    key="${item%%|*}"; expected="${item##*|}"
    actual="$(env_get "$file" "$key")"
    if [ "$actual" != "$expected" ]; then
      warn "$key 必须为 $expected（当前：${actual:-<空>}）"; problems=$((problems+1))
    fi
  done

  local tenant_id instance_id
  tenant_id="$(env_get "$file" TENANT_ID)"
  instance_id="$(env_get "$file" INSTANCE_ID)"
  is_positive_int "$tenant_id" || { warn "TENANT_ID 必须是正整数（当前：${tenant_id:-<空>}）"; problems=$((problems+1)); }
  is_positive_int "$instance_id" || { warn "INSTANCE_ID 必须是正整数（当前：${instance_id:-<空>}）"; problems=$((problems+1)); }
  [ "$(env_get "$file" ERP_WORKER_TENANT_ID)" = "$tenant_id" ] || { warn "ERP_WORKER_TENANT_ID 必须等于 TENANT_ID"; problems=$((problems+1)); }
  [ "$(env_get "$file" ERP_WORKER_INSTANCE_ID)" = "$instance_id" ] || { warn "ERP_WORKER_INSTANCE_ID 必须等于 INSTANCE_ID"; problems=$((problems+1)); }
  [ "$(env_get "$file" ERP_WORKER_STRICT_BINDING)" = "true" ] || { warn "ERP_WORKER_STRICT_BINDING 必须为 true"; problems=$((problems+1)); }
  if [ -n "$(env_get "$file" BACKUP_INSTANCE_ID)" ] && [ "$(env_get "$file" BACKUP_INSTANCE_ID)" != "$instance_id" ]; then
    warn "BACKUP_INSTANCE_ID 必须等于 INSTANCE_ID"; problems=$((problems+1))
  fi

  local origin cookie_secure project
  origin="$(env_get "$file" APP_ORIGIN)"
  cookie_secure="$(env_get "$file" SESSION_COOKIE_SECURE)"
  project="$(env_get "$file" COMPOSE_PROJECT_NAME)"
  [[ "$origin" == https://* ]] || { warn "APP_ORIGIN 必须是 https 地址（当前：${origin:-<空>}）"; problems=$((problems+1)); }
  [ "$cookie_secure" = "true" ] || { warn "SESSION_COOKIE_SECURE 必须为 true"; problems=$((problems+1)); }
  [ -n "$project" ] || { warn "COMPOSE_PROJECT_NAME 不能为空"; problems=$((problems+1)); }
  case "$project" in *drill*|ventasync) warn "COMPOSE_PROJECT_NAME 不能与中心/演练项目相同：$project"; problems=$((problems+1));; esac
  [ -n "$(env_get "$file" TRUSTED_HOSTS)" ] || { warn "TRUSTED_HOSTS 不能为空"; problems=$((problems+1)); }
  local image_tag
  image_tag="$(env_get "$file" APP_IMAGE_TAG)"
  case "$image_tag" in ""|local|__SET_ME__) warn "APP_IMAGE_TAG 应为本次发布版本/tag（当前：${image_tag:-<空>}）"; problems=$((problems+1));; esac
  grep -Eq '^[^#]*__SET_ME__' "$file" && { warn "环境文件仍有 __SET_ME__ 占位符"; problems=$((problems+1)); }

  local perms
  perms="$(stat -c '%a' "$file" 2>/dev/null || echo '')"
  [ "$perms" = "600" ] || warn "建议 chmod 600 $file（当前：${perms:-unknown}）"
  if [ "$problems" -gt 0 ]; then
    die "环境校验失败：$problems 项（修复后重跑 config.sh --check）"
  fi
  log "环境校验通过：tenant_id=${tenant_id} instance_id=${instance_id} project=${project} origin=${origin}"
}

render_env() {
  [ -n "$TENANT_ID" ] && [ -n "$INSTANCE_ID" ] && [ -n "$DOMAIN" ] || die "--render 需要 --tenant-id/--instance-id/--domain"
  is_positive_int "$TENANT_ID" || die "--tenant-id 必须是正整数"
  is_positive_int "$INSTANCE_ID" || die "--instance-id 必须是正整数"
  [ -n "$OUTPUT" ] || die "--render 需要 --output"
  [ -n "$PROJECT" ] || PROJECT="ventasync-tenant-${TENANT_ID}"
  [ -n "$IMAGE_TAG" ] || die "--render 需要 --image-tag（发布版本/commit）"
  [ -e "$OUTPUT" ] && [ "$FORCE" -ne 1 ] && die "输出文件已存在：$OUTPUT（如需覆盖请 --force，并先自行备份）"
  [ -f "$ENV_TEMPLATE" ] || die "模板不存在：$ENV_TEMPLATE"
  require_cmd openssl
  [ -n "$MYSQL_ROOT_PASSWORD" ] || MYSQL_ROOT_PASSWORD="$(openssl rand -hex 24)"
  [ -n "$DB_PASSWORD" ] || DB_PASSWORD="$(openssl rand -hex 24)"
  local origin="https://${DOMAIN}"
  local tmp
  tmp="$(mktemp "${OUTPUT}.tmp.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  umask 077
  while IFS= read -r line || [ -n "$line" ]; do
    key="${line%%=*}"
    if [[ "$line" == *=* ]]; then
      case "$key" in
        COMPOSE_PROJECT_NAME) line="COMPOSE_PROJECT_NAME=${PROJECT}" ;;
        TENANT_ID|ERP_WORKER_TENANT_ID) line="${key}=${TENANT_ID}" ;;
        INSTANCE_ID|ERP_WORKER_INSTANCE_ID|BACKUP_INSTANCE_ID) line="${key}=${INSTANCE_ID}" ;;
        APP_IMAGE_TAG) line="APP_IMAGE_TAG=${IMAGE_TAG}" ;;
        APP_ORIGIN|CORS_ALLOW_ORIGINS) line="${key}=${origin}" ;;
        TRUSTED_HOSTS) line="TRUSTED_HOSTS=${DOMAIN}" ;;
        SHOPEE_AUTH_REDIRECT_URL) line="SHOPEE_AUTH_REDIRECT_URL=${origin}/api/v1/integrations/shopee/oauth/callback" ;;
        TIKTOK_AUTH_REDIRECT_URL) line="TIKTOK_AUTH_REDIRECT_URL=${origin}/api/v1/integrations/tiktok/oauth/callback" ;;
        MYSQL_ROOT_PASSWORD) line="MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD}" ;;
        CENTER_DB_PASSWORD) line="CENTER_DB_PASSWORD=${DB_PASSWORD}" ;;
        GATEWAY_BASE_URL) line="GATEWAY_BASE_URL=${GATEWAY_URL}" ;;
        *) line="${line//__SET_ME_OR_EMPTY__/}" ;;
      esac
    fi
    printf '%s\n' "$line" >> "$tmp"
  done < "$ENV_TEMPLATE"
  if grep -q '__SET_ME' "$tmp"; then
    die "模板仍有未替换占位符，请检查模板/参数"
  fi
  if [ -e "$OUTPUT" ] && [ "$FORCE" -eq 1 ]; then
    cp -a "$OUTPUT" "${OUTPUT}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    warn "已备份原文件：${OUTPUT}.bak.*"
  fi
  install -m 600 -o root -g root "$tmp" "$OUTPUT" 2>/dev/null || install -m 600 "$tmp" "$OUTPUT"
  chmod 600 "$OUTPUT"
  rm -f "$tmp"; trap - EXIT
  log "已生成：$OUTPUT（权限 600；密码未打印，请自行安全保管）"
  validate_env "$OUTPUT"
}

case "$MODE" in
  check) validate_env "$ENV_FILE" ;;
  render) render_env ;;
  *) usage; exit 2 ;;
esac
