#!/usr/bin/env bash
# =============================================================================
# VentaSync 单库恢复脚本（mysqldump 备份恢复）
#
# 用法：
#   deploy/scripts/restore.sh -f deploy/backups/vs_tenant_1_20260101T000000Z.sql.gz \
#       -d vs_tenant_1 --tenant-id 1 --yes
#   deploy/scripts/restore.sh -f <中心库备份> -d ventasync_center --allow-center --yes
#
# 安全约定（plan.txt §5 验证项 9）：
#   1. 只恢复单个库：租户库恢复不影响其他租户；恢复中心库需额外 --allow-center。
#   2. 恢复是破坏性操作：先 DROP 再 CREATE 目标库，必须 --yes 确认；--dry-run 只打印计划。
#   3. 租户处于 active 时拒绝恢复（先用平台功能暂停租户），避免并发写入。
#   4. 租户库恢复后自动关闭 stock_push_enabled / order_sync_enabled：
#      「恢复后先对账，再开启平台推送」。
#   5. 备份旁路存在 .sha256 时先校验完整性。
#   6. 恢复完成后需重启 api / worker / scheduler，清空租户 Engine 缓存与会话缓存。
#
# 环境变量来源：deploy/.env（存在时自动加载），可用同名环境变量覆盖。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DEPLOY_DIR}/.." && pwd)"
COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.yml"

# ---------------------------------------------------------------- 环境变量
if [[ -f "${DEPLOY_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${DEPLOY_DIR}/.env"
    set +a
fi

: "${CENTER_DB_NAME:=ventasync_center}"
: "${TENANT_DB_PREFIX:=vs_tenant_}"

BACKUP_FILE=""
DB_NAME=""
TENANT_ID=""
ASSUME_YES=0
ALLOW_CENTER=0
FORCE_ACTIVE=0
DRY_RUN=0

log() { printf '[restore] %s\n' "$*"; }
warn() { printf '[restore] 警告：%s\n' "$*" >&2; }
die() { printf '[restore] 错误：%s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

参数：
  -f, --file <文件>      备份文件（.sql.gz）
  -d, --database <库名>  恢复目标库（中心库或 vs_tenant_* 租户库）
  -t, --tenant-id <ID>   租户 ID（租户库恢复建议提供：校验租户状态、关联核对）
      --yes              跳过交互确认（自动化用）
      --allow-center     允许恢复中心库（影响全部租户，谨慎）
      --force-active     跳过「租户 active 拒绝恢复」检查（不建议）
      --dry-run          只打印计划，不做任何修改
  -h, --help             显示帮助
EOF
}

# ---------------------------------------------------------------- 参数解析
while [[ $# -gt 0 ]]; do
    case "$1" in
        -f|--file) [[ $# -ge 2 ]] || die "--file 需要参数"; BACKUP_FILE="$2"; shift 2 ;;
        -d|--database) [[ $# -ge 2 ]] || die "--database 需要参数"; DB_NAME="$2"; shift 2 ;;
        -t|--tenant-id) [[ $# -ge 2 ]] || die "--tenant-id 需要参数"; TENANT_ID="$2"; shift 2 ;;
        --yes) ASSUME_YES=1; shift ;;
        --allow-center) ALLOW_CENTER=1; shift ;;
        --force-active) FORCE_ACTIVE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1（使用 -h 查看帮助）" ;;
    esac
done

# ---------------------------------------------------------------- 前置校验
[[ -n "${BACKUP_FILE}" ]] || die "必须用 -f 指定备份文件"
[[ -n "${DB_NAME}" ]] || die "必须用 -d 指定恢复目标库"
[[ -f "${BACKUP_FILE}" ]] || die "备份文件不存在：${BACKUP_FILE}"
[[ "${DB_NAME}" =~ ^[A-Za-z0-9_]+$ ]] || die "非法库名：${DB_NAME}"
[[ -z "${TENANT_ID}" || "${TENANT_ID}" =~ ^[0-9]+$ ]] || die "--tenant-id 必须是数字"

command -v docker >/dev/null 2>&1 || die "未找到 docker 命令"
command -v gzip >/dev/null 2>&1 || die "未找到 gzip 命令"
gzip -t "${BACKUP_FILE}" || die "备份文件不是有效的 gzip：${BACKUP_FILE}"

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        printf 'unavailable'
    fi
}

if [[ -f "${BACKUP_FILE}.sha256" ]]; then
    expected="$(awk '{print $1}' "${BACKUP_FILE}.sha256")"
    actual="$(sha256_of "${BACKUP_FILE}")"
    [[ "${expected}" == "${actual}" ]] \
        || die "备份文件校验和不匹配（期望 ${expected}，实际 ${actual}）"
    log "备份文件 sha256 校验通过：${actual}"
else
    warn "未找到 ${BACKUP_FILE}.sha256，跳过完整性校验"
fi

IS_CENTER=0
if [[ "${DB_NAME}" == "${CENTER_DB_NAME}" ]]; then
    IS_CENTER=1
    if [[ "${ALLOW_CENTER}" -ne 1 ]]; then
        die "拒绝恢复中心库：将影响全部租户。确认后请追加 --allow-center"
    fi
elif [[ "${DB_NAME}" == "${TENANT_DB_PREFIX}"* ]]; then
    IS_CENTER=0
else
    die "只允许恢复中心库（${CENTER_DB_NAME}）或租户库（前缀 ${TENANT_DB_PREFIX}），收到：${DB_NAME}"
fi

[[ -f "${COMPOSE_FILE}" ]] || die "找不到 Compose 文件：${COMPOSE_FILE}"
compose() { docker compose -f "${COMPOSE_FILE}" "$@"; }
compose ps --status running --services 2>/dev/null | grep -qx "mysql" \
    || die "mysql 服务未运行，请先执行：docker compose -f deploy/docker-compose.yml up -d mysql"

# 租户 active 时不允许恢复（先通过平台暂停租户，禁止业务访问与主动同步）
if [[ "${IS_CENTER}" -eq 0 && -n "${TENANT_ID}" ]]; then
    tenant_status="$(
        compose exec -T -e TENANT_STATUS_DB="${CENTER_DB_NAME}" -e TENANT_STATUS_ID="${TENANT_ID}" mysql sh -c '
            set -eu
            : "${TENANT_STATUS_DB:?}" "${TENANT_STATUS_ID:?}"
            export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD 未设置}"
            mysql -N -B -uroot -e \
              "SELECT status FROM \`${TENANT_STATUS_DB}\`.tenants WHERE id = ${TENANT_STATUS_ID} LIMIT 1"
        ' 2>/dev/null || true
    )"
    if [[ -z "${tenant_status}" ]]; then
        warn "中心库未查到 tenant_id=${TENANT_ID} 的状态，请人工确认租户已暂停"
    elif [[ "${tenant_status}" == "active" && "${FORCE_ACTIVE}" -ne 1 ]]; then
        die "租户 tenant_id=${TENANT_ID} 仍为 active：请先在平台暂停租户（禁止业务访问与主动同步）后重试，或确认风险后追加 --force-active"
    else
        log "租户 tenant_id=${TENANT_ID} 状态：${tenant_status}"
    fi
fi

# ---------------------------------------------------------------- 计划确认
print_plan() {
    cat <<EOF
[restore] 恢复计划：
[restore]   备份文件：${BACKUP_FILE}
[restore]   目标库  ：${DB_NAME}$([[ "${IS_CENTER}" -eq 1 ]] && printf '（中心库）' || printf '（租户库）')
[restore]   租户 ID ：${TENANT_ID:-未提供}
[restore]   操作    ：DROP DATABASE ${DB_NAME} → CREATE DATABASE → 导入备份
[restore]   恢复后  ：校验完整性 → 输出对账基数$([[ "${IS_CENTER}" -eq 0 ]] && printf ' → 关闭平台推送与订单同步')
EOF
}

print_plan
if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "--dry-run：未做任何修改"
    exit 0
fi

if [[ "${ASSUME_YES}" -ne 1 ]]; then
    [[ -t 0 ]] || die "非交互环境必须追加 --yes 才能执行恢复"
    printf '[restore] 确认删除并重建数据库 %s？输入库名确认：' "${DB_NAME}"
    read -r answer
    [[ "${answer}" == "${DB_NAME}" ]] || die "确认失败，已取消"
fi

# ---------------------------------------------------------------- 重建目标库
log "重建目标库 ${DB_NAME}（DROP + CREATE）"
compose exec -T -e TARGET_DB="${DB_NAME}" mysql sh -c '
    set -eu
    : "${TARGET_DB:?TARGET_DB 未设置}"
    export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD 未设置}"
    mysql -uroot -e \
      "DROP DATABASE IF EXISTS \`${TARGET_DB}\`; CREATE DATABASE \`${TARGET_DB}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
'

# ---------------------------------------------------------------- 导入备份
log "导入备份（此步骤可能持续数分钟，请勿中断）"
if ! gzip -dc "${BACKUP_FILE}" | compose exec -T mysql sh -c '
    set -eu
    export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD 未设置}"
    exec mysql -uroot --max-allowed-packet=64M
'; then
    die "导入失败：目标库 ${DB_NAME} 可能处于不完整状态，请重新执行恢复"
fi
log "导入完成"

# ---------------------------------------------------------------- 恢复后校验
check_sql() {
    local label="$1" sql="$2"
    local value
    value="$(compose exec -T -e CHECK_DB="${DB_NAME}" -e CHECK_SQL="${sql}" mysql sh -c '
        set -eu
        : "${CHECK_DB:?}" "${CHECK_SQL:?}"
        export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD 未设置}"
        mysql -N -B -uroot -e "USE \`${CHECK_DB}\`; ${CHECK_SQL}"
    ' 2>/dev/null || echo "查询失败")"
    printf '[restore]   %-24s %s\n' "${label}" "${value}"
}

log "恢复后校验："
check_sql "数据表数量" "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE()"
check_sql "Alembic 版本" "SELECT COALESCE(MAX(version_num), '(空)') FROM alembic_version"

if [[ "${IS_CENTER}" -eq 1 ]]; then
    log "中心库对账基数（供人工核对）："
    for sql in \
        "SELECT COUNT(*) FROM users" \
        "SELECT COUNT(*) FROM tenants" \
        "SELECT COUNT(*) FROM tenant_memberships" \
        "SELECT COUNT(*) FROM tenant_databases"
    do
        check_sql "${sql#SELECT COUNT(*) FROM }" "${sql}" || true
    done
    cat <<'EOF'

[restore] 中心库恢复后必须执行：
[restore]   1) 对照恢复前的租户清单核对 tenants / tenant_databases（库名、账号、凭据版本）；
[restore]   2) 逐租户确认状态，暂停业务后执行租户库对账；
[restore]   3) 重启 api / worker / scheduler：docker compose restart api worker scheduler；
[restore]   4) 清空/校验收件箱与任务队列中的旧消息，再按租户重新启用。
EOF
else
    log "租户库对账基数（供人工核对，非自动判定）："
    for table in orders order_items order_stock_effects inventory_balances stock_movements outbox_events; do
        check_sql "${table}" "SELECT COUNT(*) FROM \`${table}\`" || true
    done

    # 关闭平台推送与订单同步：恢复后先对账，人工确认后再开启（plan.txt §5 P6 / 验证项 9）
    log "关闭平台库存推送与订单同步（stock_push_enabled=0, order_sync_enabled=0）"
    compose exec -T -e TARGET_DB="${DB_NAME}" mysql sh -c '
        set -eu
        : "${TARGET_DB:?TARGET_DB 未设置}"
        export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD 未设置}"
        mysql -uroot -e \
          "UPDATE \`${TARGET_DB}\`.tenant_settings SET stock_push_enabled = 0, order_sync_enabled = 0, updated_at = UTC_TIMESTAMP()"
    ' || warn "tenant_settings 更新失败（备份可能来自旧版本），请手工确认推送开关"

    cat <<EOF

[restore] 恢复完成，但尚未开启平台推送。请按顺序完成对账：
  1) 核对订单：恢复库 orders / order_items 数量与平台后台、恢复前备份一致；
  2) 核对库存：inventory_balances 可用数量、stock_movements 与 order_stock_effects 的 applied/target 差额一致；
  3) 处理 outbox：SELECT * FROM outbox_events WHERE status = 'pending' ORDER BY id；
     恢复前未派发的事件可能引用已不存在的对象，需人工确认后重试或标记失败；
  4) 重启进程清空缓存：docker compose -f deploy/docker-compose.yml restart api worker scheduler；
  5) 对账通过后再开启推送（平台页面操作，或执行）：
     UPDATE ${DB_NAME}.tenant_settings SET stock_push_enabled = 1, order_sync_enabled = 1;
  6) 平台侧重新启用租户，观察库存推送与同步任务日志。
EOF
fi

cat <<'EOF'

[restore] 注意：恢复会回退备份时间之后的配置类数据（成员角色、权限、店铺凭据等），
[restore]       这些数据不会随中心库路由自动校正，请在平台上复核后再开放使用。
EOF
