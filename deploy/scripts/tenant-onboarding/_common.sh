#!/usr/bin/env bash
# 第二租户接入模板公共函数（批次 4）。
# 只提供参数/环境解析与打印，不直接修改生产；所有写操作由调用脚本显式 --apply 控制。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
DEPLOY_DIR="${REPO_ROOT}/deploy"
BACKEND_DIR="${REPO_ROOT}/backend"
WHITELIST_DEFAULT="${SCRIPT_DIR}/migration-whitelist.json"
ENV_TEMPLATE="${SCRIPT_DIR}/tenant.env.example"

log() { printf '[tenant] %s\n' "$*"; }
warn() { printf '[tenant] 警告：%s\n' "$*" >&2; }
die() { printf '[tenant] 错误：%s\n' "$*" >&2; exit 1; }

usage_preamble() {
  cat <<'EOF'
安全约定：
  * 默认 --plan（只打印，不执行）；写操作必须显式 --apply 且提供确认令牌。
  * 脚本只面向“新独立租户实例”，禁止在中心生产机/共享实例执行。
  * 不在脚本内保存密码/令牌；.env 必须 600、root 属主。
EOF
}

# 读取 .env 中的 KEY=VALUE（不 source，避免执行不可信内容）。
env_get() {
  local file="$1" key="$2" default="${3:-}"
  [ -f "$file" ] || { printf '%s' "$default"; return 0; }
  local value
  value="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -n 1 | cut -d= -f2- || true)"
  value="${value%$'\r'}"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  if [ -z "$value" ]; then printf '%s' "$default"; else printf '%s' "$value"; fi
}

require_env() {
  local file="$1"; shift
  [ -f "$file" ] || die "环境文件不存在：$file"
  local key value
  for key in "$@"; do
    value="$(env_get "$file" "$key")"
    [ -n "$value" ] || die "环境文件缺少必填项：$key"
    case "$value" in
      *REPLACE_ME*|*CHANGE_ME*|*example.com*) die "$key 仍是占位符：$value" ;;
    esac
  done
}

confirm_or_die() {
  local expected="$1" actual="${2:-}"
  if [ "$actual" != "$expected" ]; then
    die "确认令牌不匹配：需要 --confirm ${expected}（当前：${actual:-<空>}）"
  fi
}

compose_cmd() {
  local env_file="$1"; shift
  docker compose \
    -f "${DEPLOY_DIR}/docker-compose.yml" \
    -f "${DEPLOY_DIR}/docker-compose.server.yml" \
    --env-file "$env_file" "$@"
}

require_cmd() {
  local name
  for name in "$@"; do
    command -v "$name" >/dev/null 2>&1 || die "缺少命令：$name"
  done
}

assert_not_drill_project() {
  local env_file="$1"
  local project
  project="$(env_get "$env_file" COMPOSE_PROJECT_NAME)"
  case "$project" in
    *drill*) die "COMPOSE_PROJECT_NAME 不能包含 drill：$project" ;;
    "") warn "COMPOSE_PROJECT_NAME 为空，compose 将使用目录默认名（建议显式设置）" ;;
  esac
}

run_or_echo() {
  if [ "${APPLY:-0}" = "1" ]; then
    "$@"
  else
    printf '[plan] %s\n' "$*"
  fi
}
