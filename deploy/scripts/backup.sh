#!/usr/bin/env bash
# =============================================================================
# VentaSync 单库备份脚本（mysqldump）
#
# 用法（在仓库根目录或任意目录执行）：
#   deploy/scripts/backup.sh -d ventasync_center        # 备份中心库
#   deploy/scripts/backup.sh -d vs_tenant_1             # 备份单个租户库
#   deploy/scripts/backup.sh --all-tenants              # 遍历中心库 tenant_databases 逐个单库备份
#   deploy/scripts/backup.sh -d vs_tenant_1 -o /data/bak -k 30
#
# 约定（plan.txt §5 验证项 9）：
#   * 只做「单库」mysqldump，备份/恢复单个租户库不影响其他租户；
#   * 备份文件写入 BACKUP_DIR（默认 deploy/backups），文件名 <库名>_<UTC时间>.sql.gz；
#   * 同时生成 .sha256 校验文件，恢复前可用于完整性核对；
#   * 本脚本不启动/停止容器，需要 mysql 服务已在运行（docker compose up -d mysql）。
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
: "${BACKUP_DIR:=backups}"
: "${BACKUP_RETENTION_DAYS:=14}"

DB_NAME=""
ALL_TENANTS=0
OUT_DIR=""
KEEP_DAYS="${BACKUP_RETENTION_DAYS}"

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

参数：
  -d, --database <库名>   备份指定数据库（单库）
  -a, --all-tenants       从中心库 tenant_databases 读取租户库并逐个备份
  -o, --output-dir <目录> 备份输出目录（默认 deploy/backups，可为绝对路径）
  -k, --keep-days <天数>  清理超过天数的旧备份（默认 BACKUP_RETENTION_DAYS=14，0=不清理）
  -h, --help              显示帮助
EOF
}

log() { printf '[backup] %s\n' "$*"; }
die() { printf '[backup] 错误：%s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 参数解析
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--database)
            [[ $# -ge 2 ]] || die "--database 需要参数"
            DB_NAME="$2"; shift 2 ;;
        -a|--all-tenants)
            ALL_TENANTS=1; shift ;;
        -o|--output-dir)
            [[ $# -ge 2 ]] || die "--output-dir 需要参数"
            OUT_DIR="$2"; shift 2 ;;
        -k|--keep-days)
            [[ $# -ge 2 ]] || die "--keep-days 需要参数"
            KEEP_DAYS="$2"; shift 2 ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            die "未知参数：$1（使用 -h 查看帮助）" ;;
    esac
done

[[ -z "${DB_NAME}" && "${ALL_TENANTS}" -eq 0 ]] && die "请用 -d 指定库名，或使用 --all-tenants"
[[ -n "${DB_NAME}" && "${ALL_TENANTS}" -eq 1 ]] && die "-d 与 --all-tenants 不能同时使用"
[[ "${KEEP_DAYS}" =~ ^[0-9]+$ ]] || die "--keep-days 必须是非负整数"

if [[ -z "${OUT_DIR}" ]]; then
    OUT_DIR="${BACKUP_DIR}"
fi
if [[ "${OUT_DIR}" != /* ]]; then
    OUT_DIR="${REPO_ROOT}/${OUT_DIR}"
fi
mkdir -p "${OUT_DIR}"
umask 077

[[ -n "${CENTER_DB_NAME}" && "${CENTER_DB_NAME}" =~ ^[A-Za-z0-9_]+$ ]] \
    || die "CENTER_DB_NAME 非法：${CENTER_DB_NAME:-<空>}"
[[ -f "${COMPOSE_FILE}" ]] || die "找不到 Compose 文件：${COMPOSE_FILE}"

command -v docker >/dev/null 2>&1 || die "未找到 docker 命令"
command -v gzip >/dev/null 2>&1 || die "未找到 gzip 命令"

compose() { docker compose -f "${COMPOSE_FILE}" "$@"; }

compose ps --status running --services 2>/dev/null | grep -qx "mysql" \
    || die "mysql 服务未运行，请先执行：docker compose -f deploy/docker-compose.yml up -d mysql"

# ---------------------------------------------------------------- 备份单个库
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        printf 'unavailable'
    fi
}

dump_one() {
    local db="$1"
    [[ "${db}" =~ ^[A-Za-z0-9_]+$ ]] || die "非法库名：${db}（只允许字母、数字、下划线）"

    local ts file tmp digest
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    file="${OUT_DIR}/${db}_${ts}.sql.gz"
    tmp="${file}.part"

    log "开始备份 ${db} -> ${file}"
    # --single-transaction：InnoDB 一致性快照，不锁表；--databases：带 CREATE DATABASE / USE，
    # 便于恢复时核对库名；--set-gtid-purged=OFF 避免 GTID 环境恢复报错。
    if ! compose exec -T -e DUMP_DB="${db}" mysql sh -c '
        set -eu
        : "${DUMP_DB:?DUMP_DB 未设置}"
        export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD 未设置}"
        exec mysqldump -uroot \
            --single-transaction \
            --quick \
            --hex-blob \
            --routines \
            --triggers \
            --default-character-set=utf8mb4 \
            --set-gtid-purged=OFF \
            --no-tablespaces \
            --databases "${DUMP_DB}"
    ' | gzip -c > "${tmp}"; then
        rm -f "${tmp}"
        die "mysqldump 失败：${db}"
    fi
    mv "${tmp}" "${file}"
    gzip -t "${file}" || { rm -f "${file}"; die "备份文件 gzip 校验失败：${file}"; }

    local size
    size="$(wc -c < "${file}" | tr -d ' ')"
    [[ "${size}" -gt 0 ]] || { rm -f "${file}"; die "备份文件为空：${file}"; }

    digest="$(sha256_of "${file}")"
    if [[ "${digest}" != "unavailable" ]]; then
        printf '%s  %s\n' "${digest}" "$(basename "${file}")" > "${file}.sha256"
    fi

    local tables
    tables="$(gzip -dc "${file}" | grep -c 'CREATE TABLE' || true)"
    if [[ "${tables}" -eq 0 ]]; then
        log "警告：${db} 备份中未发现 CREATE TABLE（空库或尚未执行迁移）"
    fi
    log "完成：$(basename "${file}")，表 ${tables} 张，大小 ${size} 字节，sha256=${digest}"
}

# ---------------------------------------------------------------- 租户库列表
tenant_databases() {
    compose exec -T -e TENANT_QUERY_DB="${CENTER_DB_NAME}" mysql sh -c '
        set -eu
        : "${TENANT_QUERY_DB:?TENANT_QUERY_DB 未设置}"
        export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD 未设置}"
        mysql -N -B -uroot -e \
          "SELECT database_name FROM \`${TENANT_QUERY_DB}\`.tenant_databases WHERE database_name IS NOT NULL AND LENGTH(database_name) > 0 ORDER BY tenant_id"
    '
}

# ---------------------------------------------------------------- 主流程
if [[ "${ALL_TENANTS}" -eq 1 ]]; then
    log "从中心库 ${CENTER_DB_NAME}.tenant_databases 读取租户库列表"
    TENANT_DBS=()
    while IFS= read -r tenant_db; do
        [[ -n "${tenant_db}" ]] && TENANT_DBS+=("${tenant_db}")
    done < <(tenant_databases)
    [[ "${#TENANT_DBS[@]}" -gt 0 ]] || die "未读取到任何租户库（tenant_databases 为空？）"
    log "共 ${#TENANT_DBS[@]} 个租户库：${TENANT_DBS[*]}"
    for db in "${TENANT_DBS[@]}"; do
        dump_one "${db}"
    done
else
    dump_one "${DB_NAME}"
fi

# ---------------------------------------------------------------- 清理旧备份
if [[ "${KEEP_DAYS}" -gt 0 ]]; then
    log "清理 ${OUT_DIR} 中超过 ${KEEP_DAYS} 天的备份"
    find "${OUT_DIR}" -maxdepth 1 -type f \
        \( -name '*.sql.gz' -o -name '*.sql.gz.sha256' \) \
        -mtime "+${KEEP_DAYS}" -print -delete || true
fi

log "备份目录：${OUT_DIR}"
ls -lh "${OUT_DIR}" | tail -n 20
cat <<'EOF'

后续操作：
  1) 恢复单库：deploy/scripts/restore.sh -f <备份文件> -d <库名> --yes
  2) 恢复后必须先对账（订单数 / 库存余额 / 库存流水 / outbox 积压），
     再开启平台库存推送；详见 deploy/README.md「备份与恢复」。
EOF
