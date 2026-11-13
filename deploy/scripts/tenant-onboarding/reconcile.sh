#!/usr/bin/env bash
# 停写与迁移对账模板（批次 4）：行数 / 最大ID / 稳定表指纹 / 关联检查。
#
# 用法：
#   bash reconcile.sh --plan --env-file /opt/ventasync/deploy/.env
#   bash reconcile.sh --snapshot --apply --env-file ... --output /var/tmp/reconcile-pre.tsv
#   bash reconcile.sh --compare --before pre.tsv --after post.tsv
#   sudo bash reconcile.sh --stop-write --apply --env-file ... --confirm STOP-WRITE-<TENANT_ID>
#   sudo bash reconcile.sh --resume --apply --env-file ... --confirm RESUME-<TENANT_ID>
#
# 安全：snapshot/compare 只读；stop-write/resume 只停止/启动本实例 worker+scheduler，
#       不改中心库、不操作数据；必须核对平台租户状态与维护窗口后执行。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/_common.sh"

usage() {
  cat <<'EOF'
用法：
  reconcile.sh --plan --env-file <file> [--tenant-db <db>] [--center-db <db>]
  reconcile.sh --snapshot --apply --env-file <file> --output <tsv> [--tenant-db <db>] [--center-db <db>]
  reconcile.sh --compare --before <tsv> --after <tsv>
  reconcile.sh --stop-write --apply --env-file <file> --confirm STOP-WRITE-<TENANT_ID>
  reconcile.sh --resume --apply --env-file <file> --confirm RESUME-<TENANT_ID>
EOF
}

MODE=""
ENV_FILE="${DEPLOY_DIR}/.env"
TENANT_DB=""
CENTER_DB=""
TENANT_ID=""
OUTPUT=""
BEFORE=""
AFTER=""
APPLY=0
CONFIRM=""

while [ $# -gt 0 ]; do
  case "$1" in
    --plan) MODE="plan"; shift ;;
    --snapshot) MODE="snapshot"; shift ;;
    --compare) MODE="compare"; shift ;;
    --stop-write) MODE="stop-write"; shift ;;
    --resume) MODE="resume"; shift ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --tenant-db) TENANT_DB="${2:-}"; shift 2 ;;
    --center-db) CENTER_DB="${2:-}"; shift 2 ;;
    --output) OUTPUT="${2:-}"; shift 2 ;;
    --before) BEFORE="${2:-}"; shift 2 ;;
    --after) AFTER="${2:-}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --confirm) CONFIRM="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

usage_preamble
[ -n "$MODE" ] || die "缺少模式（--plan/--snapshot/--compare/--stop-write/--resume）"

resolve_names() {
  TENANT_ID="$(env_get "$ENV_FILE" TENANT_ID)"
  [ -n "$TENANT_DB" ] || TENANT_DB="$(env_get "$ENV_FILE" TARGET_TENANT_DB)"
  [ -n "$CENTER_DB" ] || CENTER_DB="$(env_get "$ENV_FILE" CENTER_DB_NAME ventasync_center)"
  if [ -z "$TENANT_DB" ]; then
    TENANT_DB="$(python3 - "$WHITELIST_DEFAULT" "$TENANT_ID" <<'PY'
import json, sys
whitelist = json.load(open(sys.argv[1], encoding="utf-8"))
for item in whitelist.get("allowed_tenants") or []:
    if str(item.get("tenant_id")) == sys.argv[2]:
        print(item.get("database_name") or "")
        break
PY
)"
  fi
  [ -n "$TENANT_ID" ] || die "环境文件缺少 TENANT_ID"
  [ -n "$TENANT_DB" ] || die "缺少租户库名：--tenant-db 或白名单 allowed_tenants[].database_name"
}

run_sql_snapshot() {
  local sql_file="$1" out_file="$2"
  # 优先在 api 容器内生成 SQL（有模型元数据，指纹覆盖全部列）；白名单临时复制，完成后删除
  local container_whitelist="/tmp/tenant-migration-whitelist.$$.json"
  compose_cmd "$ENV_FILE" cp "$WHITELIST_DEFAULT" "api:${container_whitelist}" >/dev/null
  compose_cmd "$ENV_FILE" exec -T api python scripts/tenant_migration_whitelist.py emit-sql \
    --whitelist "$container_whitelist" --tenant-id "$TENANT_ID" \
    --tenant-db "$TENANT_DB" --center-db "$CENTER_DB" --phase "${PHASE:-pre}" > "$sql_file"
  compose_cmd "$ENV_FILE" exec -T api rm -f "$container_whitelist" >/dev/null 2>&1 || true
  compose_cmd "$ENV_FILE" exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N -B' \
    < "$sql_file" > "$out_file"
  [ -s "$out_file" ] || die "对账快照为空：$out_file"
  log "对账快照：${out_file}（$(wc -l < "$out_file") 行）"
}

case "$MODE" in
  plan)
    resolve_names
    cat <<EOF
[plan] 租户：tenant_id=${TENANT_ID} tenant_db=${TENANT_DB} center_db=${CENTER_DB}
[plan] 停写顺序（人工确认维护窗口）：
  1) 平台暂停该租户（或确认业务低峰）；
  2) reconcile.sh --stop-write --apply --confirm STOP-WRITE-${TENANT_ID}
  3) reconcile.sh --snapshot --apply --output /var/tmp/reconcile-pre.tsv
  4) 执行 migrate.sh --apply --stop-write（迁移）
  5) reconcile.sh --snapshot --apply --output /var/tmp/reconcile-post.tsv
  6) reconcile.sh --compare --before pre.tsv --after post.tsv
  7) reconcile.sh --resume --apply --confirm RESUME-${TENANT_ID}
[plan] 对账口径：稳定表完全一致；追加型表只允许增长；关联检查必须为 0。
EOF
    ;;
  snapshot)
    [ "$APPLY" -eq 1 ] || die "--snapshot 需要 --apply 才执行（默认只打印计划）"
    [ -n "$OUTPUT" ] || die "--snapshot 需要 --output <tsv>"
    require_cmd docker python3
    resolve_names
    assert_not_drill_project "$ENV_FILE"
    mkdir -p "$(dirname "$OUTPUT")"
    sql_file="$(mktemp /var/tmp/reconcile-XXXXXX.sql)"
    trap 'rm -f "$sql_file"' EXIT
    PHASE="pre"; [[ "$OUTPUT" == *post* ]] && PHASE="post"
    run_sql_snapshot "$sql_file" "$OUTPUT"
    ;;
  compare)
    [ -n "$BEFORE" ] && [ -n "$AFTER" ] || die "--compare 需要 --before 与 --after"
    [ -f "$BEFORE" ] && [ -f "$AFTER" ] || die "对账快照不存在"
    require_cmd python3
    python3 "${REPO_ROOT}/backend/scripts/tenant_migration_whitelist.py" compare-tsv \
      --whitelist "$WHITELIST_DEFAULT" --before "$BEFORE" --after "$AFTER"
    ;;
  stop-write|resume)
    [ "$APPLY" -eq 1 ] || die "--${MODE} 需要 --apply 才执行"
    resolve_names
    assert_not_drill_project "$ENV_FILE"
    if [ "$MODE" = "stop-write" ]; then
      confirm_or_die "STOP-WRITE-${TENANT_ID}" "$CONFIRM"
      log "停止本实例 worker/scheduler（API 保持只读运行；请先确认平台已暂停租户）"
      compose_cmd "$ENV_FILE" stop worker scheduler
      log "已停止。若迁移失败/需要恢复：reconcile.sh --resume --apply --confirm RESUME-${TENANT_ID}"
    else
      confirm_or_die "RESUME-${TENANT_ID}" "$CONFIRM"
      log "启动本实例 worker/scheduler"
      compose_cmd "$ENV_FILE" start worker scheduler
      log "已启动；请检查 /api/v1/healthz 与监控分项状态。"
    fi
    ;;
  *) die "未知模式：$MODE" ;;
esac
