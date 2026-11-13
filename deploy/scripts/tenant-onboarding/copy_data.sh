#!/usr/bin/env bash
# 第二租户数据复制模板（批次 4）：按白名单复制租户库 + 租户范围中心库记录。
#
# 先决条件（缺一不可）：
#   * 源库已停写（平台已暂停租户/维护窗口），且已做迁移前备份；
#   * 目标库已完成 Alembic 迁移（migrate.sh --plan/--apply 的 schema 部分）；
#   * 源/目标 MySQL 客户端可用，凭据放在 600 权限 option file，不写命令行；
#   * 白名单只复制 tenant.migrate!=false 的表与中心库 6 张表；内部 ID/member_no 原样保留。
#
# 用法（默认 plan，只打印 dump/import 命令）：
#   bash copy_data.sh --plan --env-file /opt/ventasync/deploy/.env \
#     --source-defaults-file /root/.my-source.cnf --source-tenant-db vs_tenant_kangkang_shop \
#     [--source-center-db ventasync_center] [--dump-dir /var/tmp/tenant-copy]
#
#   sudo bash copy_data.sh --apply --confirm COPY-<TENANT_ID>-<SOURCE_TENANT_DB> \
#     --source-defaults-file ... --target-defaults-file ... ...
#
# 安全：不打印密码；dump 文件含业务数据，用完按数据安全要求删除；--apply 不自动清空目标库。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/_common.sh"

usage() {
  cat <<'EOF'
用法：
  copy_data.sh --plan  --env-file <file> --source-defaults-file <cnf> --source-tenant-db <db> \
      [--source-center-db ventasync_center] [--target-center-db ventasync_center] \
      [--target-tenant-db <db>] [--dump-dir /var/tmp/tenant-copy]
  sudo copy_data.sh --apply --confirm COPY-<TENANT_ID>-<SOURCE_TENANT_DB> \
      --source-defaults-file <cnf> --target-defaults-file <cnf> ...（同 plan 参数）

说明：--apply 执行 mysqldump → mysql 导入；不自动停写、不自动清空目标库。
EOF
}

ENV_FILE="${DEPLOY_DIR}/.env"
SOURCE_DEFAULTS=""
TARGET_DEFAULTS=""
SOURCE_TENANT_DB=""
SOURCE_CENTER_DB="ventasync_center"
TARGET_CENTER_DB=""
TARGET_TENANT_DB=""
DUMP_DIR="/var/tmp/tenant-copy"
APPLY=0
CONFIRM=""
WHITELIST="$WHITELIST_DEFAULT"

while [ $# -gt 0 ]; do
  case "$1" in
    --plan) APPLY=0; shift ;;
    --apply) APPLY=1; shift ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --source-defaults-file) SOURCE_DEFAULTS="${2:-}"; shift 2 ;;
    --target-defaults-file) TARGET_DEFAULTS="${2:-}"; shift 2 ;;
    --source-tenant-db) SOURCE_TENANT_DB="${2:-}"; shift 2 ;;
    --source-center-db) SOURCE_CENTER_DB="${2:-}"; shift 2 ;;
    --target-center-db) TARGET_CENTER_DB="${2:-}"; shift 2 ;;
    --target-tenant-db) TARGET_TENANT_DB="${2:-}"; shift 2 ;;
    --dump-dir) DUMP_DIR="${2:-}"; shift 2 ;;
    --confirm) CONFIRM="${2:-}"; shift 2 ;;
    --whitelist) WHITELIST="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

usage_preamble
TENANT_ID="$(env_get "$ENV_FILE" TENANT_ID)"
INSTANCE_ID="$(env_get "$ENV_FILE" INSTANCE_ID)"
[ -n "$TENANT_ID" ] && [ -n "$INSTANCE_ID" ] || die "环境文件缺少 TENANT_ID/INSTANCE_ID"
[ -n "$SOURCE_TENANT_DB" ] || die "缺少 --source-tenant-db"
[ -n "$SOURCE_DEFAULTS" ] || die "缺少 --source-defaults-file（600 权限 MySQL option file）"
[ -n "$TARGET_CENTER_DB" ] || TARGET_CENTER_DB="$(env_get "$ENV_FILE" CENTER_DB_NAME ventasync_center)"
[ -n "$TARGET_TENANT_DB" ] || TARGET_TENANT_DB="$SOURCE_TENANT_DB"
CONFIRM_TOKEN="COPY-${TENANT_ID}-${SOURCE_TENANT_DB}"

# 白名单表清单/中心 where 条件（只读白名单文件）
mapfile -t whitelist_lines < <(python3 - "$WHITELIST" "$TENANT_ID" <<'PY'
import json, sys
whitelist = json.load(open(sys.argv[1], encoding="utf-8"))
tenant = [name for name, spec in (whitelist.get("tenant_tables") or {}).items()
          if (spec or {}).get("migrate") is not False]
center = whitelist.get("center_tables") or {}
where = {
    "tenants": f"id={sys.argv[2]}",
    "tenant_memberships": f"tenant_id={sys.argv[2]}",
    "subscriptions": f"tenant_id={sys.argv[2]}",
    "users": f"id IN (SELECT user_id FROM tenant_memberships WHERE tenant_id={sys.argv[2]})",
}
specs = [f"{name}\t{where.get(name, '')}" for name in center]
print(" ".join(tenant))
print("|".join(specs))
PY
)
TENANT_TABLES="${whitelist_lines[0]:-}"
CENTER_SPECS="${whitelist_lines[1]:-}"
[ -n "$TENANT_TABLES" ] || die "白名单未解析出可迁移表"
[[ "$SOURCE_TENANT_DB" =~ ^[A-Za-z0-9_]+$ ]] || die "源租户库名非法"
[[ "$TARGET_TENANT_DB" =~ ^[A-Za-z0-9_]+$ ]] || die "目标租户库名非法"
[[ "$SOURCE_CENTER_DB" =~ ^[A-Za-z0-9_]+$ ]] || die "源中心库名非法"
[[ "$TARGET_CENTER_DB" =~ ^[A-Za-z0-9_]+$ ]] || die "目标中心库名非法"

log "数据复制计划：tenant_id=${TENANT_ID} source_tenant=${SOURCE_TENANT_DB} target_tenant=${TARGET_TENANT_DB}"
log "白名单租户表：$(printf '%s' "$TENANT_TABLES" | wc -w) 张；中心表：$(printf '%s' "$CENTER_SPECS" | awk -F'|' '{print NF}') 张"

if [ "$APPLY" -ne 1 ]; then
  cat <<EOF
[plan] 1) 确认源库已停写、已备份；目标库 schema 已迁移（migrate.sh）
[plan] 2) 导出（--single-transaction，保留 id/tenant_id/member_no）：
  mysqldump --defaults-extra-file=${SOURCE_DEFAULTS} --single-transaction --no-create-info \\
    --complete-insert --skip-triggers --set-gtid-purged=OFF --hex-blob \\
    ${SOURCE_TENANT_DB} ${TENANT_TABLES} > ${DUMP_DIR}/tenant.sql
[plan] 3) 中心库租户范围记录（where 过滤）：
$(printf '%s' "$CENTER_SPECS" | tr '|' '\n' | awk -F'\t' '{printf "  %s%s\n", $1, ($2==""?" [全表引用数据]":" WHERE "$2)}')
[plan] 4) 导入目标（先确认目标库为空或已备份）：
  mysql --defaults-extra-file=<target.cnf> ${TARGET_TENANT_DB} < ${DUMP_DIR}/tenant.sql
[plan] 5) 对账：reconcile.sh --compare（稳定表完全一致；追加型仅增长）
[plan] 执行：sudo bash copy_data.sh --apply --confirm ${CONFIRM_TOKEN} ...（需 --target-defaults-file）
EOF
  exit 0
fi

[ "$(id -u)" -eq 0 ] || die "--apply 需要 root（sudo，读取 600 凭据文件）"
confirm_or_die "$CONFIRM_TOKEN" "$CONFIRM"
[ -n "$TARGET_DEFAULTS" ] || die "--apply 需要 --target-defaults-file"
[ -f "$SOURCE_DEFAULTS" ] && [ -f "$TARGET_DEFAULTS" ] || die "源/目标 option file 不存在"
for f in "$SOURCE_DEFAULTS" "$TARGET_DEFAULTS"; do
  mode="$(stat -c '%a' "$f")"
  [ "$mode" = "600" ] || die "凭据文件必须 600：$f（当前 ${mode}）"
done
require_cmd mysqldump mysql
mkdir -p "$DUMP_DIR"; chmod 700 "$DUMP_DIR"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
tenant_dump="${DUMP_DIR}/tenant-${ts}.sql"
center_dump="${DUMP_DIR}/center-${ts}.sql"

log "[1/4] 导出源租户库（白名单表，无建表语句）"
# shellcheck disable=SC2086
mysqldump --defaults-extra-file="$SOURCE_DEFAULTS" --single-transaction --no-create-info \
  --complete-insert --skip-triggers --set-gtid-purged=OFF --hex-blob \
  "$SOURCE_TENANT_DB" $TENANT_TABLES > "$tenant_dump"
chmod 600 "$tenant_dump"

log "[2/4] 导出中心库租户范围记录"
: > "$center_dump"
while IFS=$'\t' read -r table where; do
  [ -n "$table" ] || continue
  {
    echo "-- ${table}"
    if [ -n "$where" ]; then
      mysqldump --defaults-extra-file="$SOURCE_DEFAULTS" --single-transaction --no-create-info \
        --complete-insert --skip-triggers --set-gtid-purged=OFF --hex-blob \
        --where="$where" "$SOURCE_CENTER_DB" "$table"
    else
      mysqldump --defaults-extra-file="$SOURCE_DEFAULTS" --single-transaction --no-create-info \
        --complete-insert --skip-triggers --set-gtid-purged=OFF --hex-blob \
        "$SOURCE_CENTER_DB" "$table"
    fi
  } >> "$center_dump"
done < <(printf '%s' "$CENTER_SPECS" | tr '|' '\n')
chmod 600 "$center_dump"

log "[3/4] 导入目标租户库"
mysql --defaults-extra-file="$TARGET_DEFAULTS" "$TARGET_TENANT_DB" < "$tenant_dump"

log "[4/4] 导入目标中心库"
mysql --defaults-extra-file="$TARGET_DEFAULTS" "$TARGET_CENTER_DB" < "$center_dump"

log "复制完成：tenant=${tenant_dump} center=${center_dump}"
warn "dump 含业务数据，核对完成后按数据安全要求删除；下一步执行 reconcile.sh --compare 并人工抽查。"
