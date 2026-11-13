#!/usr/bin/env bash
# 第二租户迁移模板（批次 4）：白名单校验 → 备份 → 停写 → 对账 → Alembic 迁移 → 复核 → 恢复写入。
#
# 用法（默认 plan；apply 必须显式确认 + 已人工停写审批）：
#   bash migrate.sh --plan --env-file /opt/ventasync/deploy/.env
#   sudo bash migrate.sh --apply --stop-write --env-file /opt/ventasync/deploy/.env \
#        --confirm MIGRATE-<TENANT_ID>-<TENANT_DB> [--skip-backup]
#
# 注意：
#   * 只迁移白名单允许的租户；TENANT_ID/INSTANCE_ID/Worker 绑定必须一致；
#   * 先由平台暂停租户或确认维护窗口；停写只停本实例 worker/scheduler，API 仍只读服务；
#   * 失败不会自动恢复写入，按输出提示人工核对后 reconcile.sh --resume。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/_common.sh"

usage() {
  cat <<'EOF'
用法：
  migrate.sh --plan  --env-file <file>
  sudo migrate.sh --apply --stop-write --env-file <file> --confirm MIGRATE-<TENANT_ID>-<TENANT_DB> [--skip-backup]

默认 plan；--apply 执行：备份 → 停写 → 前快照 → alembic 迁移 → 后快照/对账 → 恢复写入。
EOF
}

ENV_FILE="${DEPLOY_DIR}/.env"
APPLY=0
STOP_WRITE=0
SKIP_BACKUP=0
CONFIRM=""
WHITELIST="$WHITELIST_DEFAULT"
REPORT_DIR="/var/tmp/tenant-migration"
while [ $# -gt 0 ]; do
  case "$1" in
    --plan) APPLY=0; shift ;;
    --apply) APPLY=1; shift ;;
    --stop-write) STOP_WRITE=1; shift ;;
    --skip-backup) SKIP_BACKUP=1; shift ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --confirm) CONFIRM="${2:-}"; shift 2 ;;
    --whitelist) WHITELIST="${2:-}"; shift 2 ;;
    --report-dir) REPORT_DIR="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

usage_preamble
[ -f "$ENV_FILE" ] || die "环境文件不存在：$ENV_FILE"
TENANT_ID="$(env_get "$ENV_FILE" TENANT_ID)"
INSTANCE_ID="$(env_get "$ENV_FILE" INSTANCE_ID)"
TENANT_DB="$(env_get "$ENV_FILE" TARGET_TENANT_DB)"
if [ -z "$TENANT_DB" ]; then
  TENANT_DB="$(python3 - "$WHITELIST" "$TENANT_ID" <<'PY'
import json, sys
whitelist = json.load(open(sys.argv[1], encoding="utf-8"))
for item in whitelist.get("allowed_tenants") or []:
    if str(item.get("tenant_id")) == sys.argv[2]:
        print(item.get("database_name") or "")
        break
PY
)"
fi
[ -n "$TENANT_ID" ] && [ -n "$INSTANCE_ID" ] || die "环境文件缺少 TENANT_ID/INSTANCE_ID"
[ -n "$TENANT_DB" ] || die "缺少租户库名（TARGET_TENANT_DB 或白名单）"
CONFIRM_TOKEN="MIGRATE-${TENANT_ID}-${TENANT_DB}"

log "租户迁移计划：tenant_id=${TENANT_ID} instance_id=${INSTANCE_ID} tenant_db=${TENANT_DB}"
if [ "$APPLY" -ne 1 ]; then
  cat <<EOF
[plan] 1) config.sh --check --env-file ${ENV_FILE}
[plan] 2) 白名单/绑定校验（tenant_migration_whitelist.py check）
[plan] 3) 备份（deploy/scripts/backup.sh --trigger manual）$( [ "$SKIP_BACKUP" -eq 1 ] && echo '（已 skip）' )
[plan] 4) 人工确认已暂停租户/维护窗口
[plan] 5) 停写：reconcile.sh --stop-write --apply --confirm STOP-WRITE-${TENANT_ID}
[plan] 6) 前对账：reconcile.sh --snapshot --apply --output ${REPORT_DIR}/reconcile-pre.tsv
[plan] 7) 迁移：docker compose exec -T api python scripts/migrate_all.py --target tenant --tenant-id ${TENANT_ID}
[plan] 8) 后对账：reconcile.sh --snapshot --apply --output ${REPORT_DIR}/reconcile-post.tsv
[plan] 9) 比较：reconcile.sh --compare --before ...-pre.tsv --after ...-post.tsv
[plan] 10) 恢复写入：reconcile.sh --resume --apply --confirm RESUME-${TENANT_ID}
[plan] 执行：sudo bash migrate.sh --apply --stop-write --env-file ${ENV_FILE} --confirm ${CONFIRM_TOKEN}
EOF
  exit 0
fi

[ "$(id -u)" -eq 0 ] || die "--apply 需要 root（sudo）"
confirm_or_die "$CONFIRM_TOKEN" "$CONFIRM"
[ "$STOP_WRITE" -eq 1 ] || die "--apply 必须显式 --stop-write（确认已获维护窗口/平台暂停）"
require_cmd docker python3
assert_not_drill_project "$ENV_FILE"
mkdir -p "$REPORT_DIR"

log "[1/8] 校验配置"
bash "${SCRIPT_DIR}/config.sh" --check --env-file "$ENV_FILE"

log "[2/8] 白名单/Worker 绑定校验"
python3 "${REPO_ROOT}/backend/scripts/tenant_migration_whitelist.py" check \
  --whitelist "$WHITELIST" --env-file "$ENV_FILE" --no-metadata

log "[3/8] 备份"
if [ "$SKIP_BACKUP" -eq 1 ]; then
  warn "已跳过备份（仅限演练；生产迁移禁止跳过）"
else
  if id -u erp-agent-exec >/dev/null 2>&1; then
    sudo -u erp-agent-exec bash "${DEPLOY_DIR}/backup.sh" --trigger manual
  else
    bash "${DEPLOY_DIR}/backup.sh" --trigger manual
  fi
fi

log "[4/8] 停写（本实例 worker/scheduler）"
bash "${SCRIPT_DIR}/reconcile.sh" --stop-write --apply --env-file "$ENV_FILE" --confirm "STOP-WRITE-${TENANT_ID}"

resume_cmd="bash ${SCRIPT_DIR}/reconcile.sh --resume --apply --env-file ${ENV_FILE} --confirm RESUME-${TENANT_ID}"
trap 'warn "迁移未完成：写入仍处于停止状态。核对后执行：'"${resume_cmd}"'"' ERR

log "[5/8] 迁移前对账快照"
bash "${SCRIPT_DIR}/reconcile.sh" --snapshot --apply --env-file "$ENV_FILE" --output "${REPORT_DIR}/reconcile-pre.tsv"

log "[6/8] Alembic 租户库迁移"
compose_cmd "$ENV_FILE" exec -T api python scripts/migrate_all.py --target tenant --tenant-id "$TENANT_ID"

log "[7/8] 迁移后对账快照"
bash "${SCRIPT_DIR}/reconcile.sh" --snapshot --apply --env-file "$ENV_FILE" --output "${REPORT_DIR}/reconcile-post.tsv"

log "[8/8] 对账比较"
bash "${SCRIPT_DIR}/reconcile.sh" --compare --before "${REPORT_DIR}/reconcile-pre.tsv" --after "${REPORT_DIR}/reconcile-post.tsv"
trap - ERR

log "恢复写入"
bash "${SCRIPT_DIR}/reconcile.sh" --resume --apply --env-file "$ENV_FILE" --confirm "RESUME-${TENANT_ID}"
log "迁移完成：tenant_id=${TENANT_ID}；证据保留在 ${REPORT_DIR}/（pre/post 快照）。"
