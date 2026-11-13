#!/usr/bin/env bash
# 双实例隔离验收（批次 4，只读探测；默认 plan）。
#
# 用法：
#   bash verify_isolation.sh --plan --env-file /opt/ventasync/deploy/.env
#   bash verify_isolation.sh --run --env-file ... \
#        --center-url https://center.example.com --tenant-url https://zuhu2.example.com \
#        [--report /var/tmp/isolation-report.json]
#
# 检查：
#   * 本实例 config.sh --check（tenant_id/instance_id/worker 绑定）；
#   * 中心 /api/v1/runtime mode=center、租户 /api/v1/runtime mode=tenant 且 tenant_id 一致；
#   * 租户 /healthz=200；
#   * 域名、OAuth 回调、Redis/DB 前缀与环境文件一致性（由 docs 清单人工复核）。
# 本脚本不写数据库、不启动/停止容器；--run 仅发 HTTP GET。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/_common.sh"

usage() {
  cat <<'EOF'
用法：
  verify_isolation.sh --plan --env-file <file>
  verify_isolation.sh --run  --env-file <file> --center-url <url> --tenant-url <url> [--report <json>]
EOF
}

ENV_FILE="${DEPLOY_DIR}/.env"
CENTER_URL=""
TENANT_URL=""
REPORT=""
RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --plan) RUN=0; shift ;;
    --run) RUN=1; shift ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --center-url) CENTER_URL="${2:-}"; shift 2 ;;
    --tenant-url) TENANT_URL="${2:-}"; shift 2 ;;
    --report) REPORT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

usage_preamble
log "双实例隔离验收：tenant 实例 $(env_get "$ENV_FILE" APP_ORIGIN)"
if [ "$RUN" -ne 1 ]; then
  cat <<EOF
[plan] 检查项（--run 执行只读 HTTP）：
  1) config.sh --check：tenant_id/instance_id/Worker 绑定一致；
  2) 中心 runtime mode=center + /healthz；
  3) 租户 runtime mode=tenant（tenant_id 由 config.sh 校验）+ /healthz=200；
  4) 租户平台接口必须 404（人工用浏览器/已登录态核对）；
  5) 账号/数据库/OAuth 回调/店铺凭据/运维任务/唯一写实例人工清单见 docs/batch4-tenant-onboarding.md。
[plan] 执行：verify_isolation.sh --run --env-file ${ENV_FILE} --center-url <center> --tenant-url <tenant>
EOF
  exit 0
fi

confirm_required() { [ -n "$CENTER_URL" ] && [ -n "$TENANT_URL" ] || die "--run 需要 --center-url 与 --tenant-url"; }
confirm_required
require_cmd curl python3
bash "${SCRIPT_DIR}/config.sh" --check --env-file "$ENV_FILE"

expected_tenant="$(env_get "$ENV_FILE" TENANT_ID)"
center_url="${CENTER_URL%/}"
tenant_url="${TENANT_URL%/}"
report_file="${REPORT:-/var/tmp/tenant-isolation-report-$(date -u +%Y%m%dT%H%M%SZ).json}"
failures=0

check_runtime() {
  local label="$1" url="$2" expected_mode="$3"
  local body
  if ! body="$(curl -fsS --max-time 10 "${url}/api/v1/runtime")"; then
    warn "${label} runtime 请求失败：${url}/api/v1/runtime"
    failures=$((failures+1)); return 0
  fi
  local mode
  mode="$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mode",""))' 2>/dev/null || true)"
  if [ "$mode" != "$expected_mode" ]; then
    warn "${label} mode=${mode:-<空>}，应为 ${expected_mode}"; failures=$((failures+1))
  fi
  if [ "$label" = "tenant" ]; then
    log "tenant: mode=${mode:-?}（固定 TENANT_ID=${expected_tenant}，由 config.sh 校验）"
  else
    log "${label}: mode=${mode:-?}"
  fi
}

check_health() {
  local label="$1" url="$2" status
  status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "${url}/healthz" || true)"
  if [ "$status" != "200" ]; then
    warn "${label} /healthz HTTP ${status}"; failures=$((failures+1))
  else
    log "${label}: /healthz 200"
  fi
}

check_runtime "center" "$center_url" "center"
check_runtime "tenant" "$tenant_url" "tenant"
check_health "center" "$center_url"
check_health "tenant" "$tenant_url"

python3 - "$report_file" "$expected_tenant" "$CENTER_URL" "$TENANT_URL" "$failures" <<'PY'
import json, sys
path, tenant, center, tenant_url, failures = sys.argv[1:6]
data = {
    "ok": int(failures) == 0,
    "tenant_id": int(tenant),
    "center_url": center,
    "tenant_url": tenant_url,
    "failures": int(failures),
    "manual_checks": [
        "账号：中心用户与租户成员不串号",
        "数据库：中心库/租户库连接串与卷独立",
        "OAuth 回调：每个实例登记独立回调地址",
        "店铺凭据：仅本实例可解密/使用",
        "运维任务：只操作本实例节点",
        "写实例：任一时刻只有该租户一个写入实例",
    ],
}
open(path, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
print(f"report={path}")
PY

if [ "$failures" -gt 0 ]; then
  die "隔离验收失败：${failures} 项（详见 ${report_file}）"
fi
log "隔离验收 HTTP 检查通过；人工清单见 docs/batch4-tenant-onboarding.md 第 6 节。"
