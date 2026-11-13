#!/usr/bin/env bash
# 第二租户开通模板（批次 4，调用中心平台 API）。
#
# 用法（默认 plan，只打印请求与后续步骤）：
#   bash provision.sh --plan --api-base https://center.example.com \
#        --code tenant2 --name "第二租户" --owner-email owner@example.com \
#        [--timezone Asia/Shanghai] [--plan-id 1]
#
#   bash provision.sh --apply --api-base https://center.example.com \
#        --cookie-file /root/.erp-admin-cookie --csrf <CSRF-TOKEN> \
#        --code tenant2 --name "第二租户" --owner-email owner@example.com \
#        --confirm PROVISION-tenant2
#
# 安全：使用平台管理员 Cookie + CSRF，只调用中心平台 API；不打印 Cookie/令牌；
#       开通后仍需在目标实例执行 config/install/migrate（本脚本不创建/迁移本地数据）。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/_common.sh"

usage() {
  cat <<'EOF'
用法：
  provision.sh --plan  --api-base <url> --code <code> --name <name> --owner-email <email> \
      [--timezone Asia/Shanghai] [--plan-id N]
  provision.sh --apply --api-base <url> --cookie-file <file> --csrf <token> \
      --code <code> --name <name> --owner-email <email> --confirm PROVISION-<code>

说明：--apply 调用 POST /api/v1/platform/tenants 并轮询开通任务；不会修改本地数据库。
EOF
}

API_BASE=""
COOKIE_FILE=""
CSRF=""
CODE=""
NAME=""
OWNER_EMAIL=""
TIMEZONE="Asia/Shanghai"
PLAN_ID=""
APPLY=0
CONFIRM=""

while [ $# -gt 0 ]; do
  case "$1" in
    --plan) APPLY=0; shift ;;
    --apply) APPLY=1; shift ;;
    --api-base) API_BASE="${2:-}"; shift 2 ;;
    --cookie-file) COOKIE_FILE="${2:-}"; shift 2 ;;
    --csrf) CSRF="${2:-}"; shift 2 ;;
    --code) CODE="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --owner-email) OWNER_EMAIL="${2:-}"; shift 2 ;;
    --timezone) TIMEZONE="${2:-}"; shift 2 ;;
    --plan-id) PLAN_ID="${2:-}"; shift 2 ;;
    --confirm) CONFIRM="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

usage_preamble
[ -n "$API_BASE" ] || die "缺少 --api-base（中心平台地址）"
[ -n "$CODE" ] || die "缺少 --code"
[ -n "$NAME" ] || die "缺少 --name"
[ -n "$OWNER_EMAIL" ] || die "缺少 --owner-email"
[[ "$CODE" =~ ^[a-z0-9_-]{2,40}$ ]] || die "--code 只能是小写字母/数字/下划线/短横线（2-40）"

payload_file="$(mktemp)"
trap 'rm -f "$payload_file"' EXIT
python3 - "$payload_file" "$CODE" "$NAME" "$TIMEZONE" "$PLAN_ID" "$OWNER_EMAIL" <<'PY'
import json, sys
path, code, name, timezone, plan_id, owner_email = sys.argv[1:7]
payload = {"code": code, "name": name, "timezone": timezone, "owner_email": owner_email}
if plan_id:
    payload["plan_id"] = int(plan_id)
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False)
PY

log "开通目标：${API_BASE} code=${CODE} owner=${OWNER_EMAIL} timezone=${TIMEZONE}"
if [ -n "$PLAN_ID" ]; then log "套餐ID：${PLAN_ID}"; else log "套餐ID：<默认套餐>"; fi

if [ "$APPLY" -ne 1 ]; then
  echo "[plan] POST ${API_BASE}/api/v1/platform/tenants"
  echo "[plan] body: $(cat "$payload_file")"
  cat <<EOF
[plan] 执行前提：
  * 中心平台管理员已登录；Cookie 文件与 CSRF 令牌来自同一会话；
  * 使用平台管理员身份调用，禁止使用租户账号；
[plan] 执行后：
  * 轮询 GET /api/v1/platform/provisioning-jobs/<id> 直至 succeeded/failed；
  * 记录返回的 tenant_id（写入目标实例 TENANT_ID）与所属 instance_id；
  * 在目标租户实例执行 config.sh --render → install.sh → migrate.sh；
  * 域名/OAuth 回调按目标实例单独登记（不得复用中心回调）。
EOF
  exit 0
fi

confirm_or_die "PROVISION-${CODE}" "$CONFIRM"
[ -n "$COOKIE_FILE" ] && [ -f "$COOKIE_FILE" ] || die "--apply 需要 --cookie-file（平台管理员会话 Cookie）"
[ -n "$CSRF" ] || die "--apply 需要 --csrf（与会话匹配的 CSRF 令牌）"
require_cmd curl python3

resp_file="$(mktemp)"
trap 'rm -f "$payload_file" "$resp_file"' EXIT
log "提交开通请求（响应不打印 Cookie/密钥）"
http_code="$(curl -sS -o "$resp_file" -w '%{http_code}' \
  -X POST "${API_BASE%/}/api/v1/platform/tenants" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: ${CSRF}" \
  -H "Origin: ${API_BASE%/}" \
  -b "$COOKIE_FILE" \
  --data-binary "@${payload_file}")"
log "HTTP ${http_code}"
case "$http_code" in
  200|201|202) ;;
  401|403) die "平台鉴权失败（检查 Cookie/CSRF 是否匹配）" ;;
  *) die "开通请求失败：HTTP ${http_code}；响应：$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("error",{}).get("message",""))' "$resp_file" 2>/dev/null || true)" ;;
esac

read -r PROV_TENANT PROV_JOB < <(python3 - "$resp_file" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8")).get("data") or {}
tenant = data.get("tenant") or {}
print(f"{tenant.get('id') or ''} {data.get('provisioning_job_id') or ''}")
PY
)
log "tenant_id=${PROV_TENANT:-?} provisioning_job_id=${PROV_JOB:-?}"

if [ -n "$PROV_JOB" ]; then
  log "轮询开通任务 /platform/provisioning-jobs/${PROV_JOB}（最多 5 分钟）"
  for _ in $(seq 1 60); do
    job_json="$(curl -sS -b "$COOKIE_FILE" -H "X-CSRF-Token: ${CSRF}" -H "Origin: ${API_BASE%/}" \
      "${API_BASE%/}/api/v1/platform/provisioning-jobs/${PROV_JOB}" 2>/dev/null || true)"
    state="$(printf '%s' "$job_json" | python3 -c '
import json,sys
try:
    data=json.load(sys.stdin).get("data") or {}
    job=data.get("job") if isinstance(data.get("job"), dict) else data
    print(str(job.get("status") or ""))
except Exception:
    pass
' 2>/dev/null || true)"
    log "开通状态：${state:-<查询中>}"
    case "$state" in succeeded|failed) break ;; esac
    sleep 5
  done
  case "${state:-}" in
    succeeded) log "开通成功；请以平台租户详情确认 status=active 并记录 instance_id" ;;
    failed) warn "开通失败：保留 provisioning job=${PROV_JOB} 日志，按平台任务重试，不得手工改库" ;;
    *) warn "轮询超时：请到平台查看租户与开通任务状态" ;;
  esac
fi

cat <<EOF
下一步（在目标租户实例）：
  1) 用返回的 tenant_id 渲染配置：config.sh --render --tenant-id <id> --instance-id <instance_id> ...
  2) config.sh --check；install.sh --plan/--apply；
  3) migrate.sh --plan → 停写对账 → migrate.sh --apply；
  4) verify_isolation.sh --run 核对双实例隔离与唯一写实例。
EOF
