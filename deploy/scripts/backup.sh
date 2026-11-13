#!/usr/bin/env bash
# =============================================================================
# ERP 租户实例备份脚本（批次3：加密异地与恢复演练）
#
# 用法：
#   bash deploy/scripts/backup.sh --trigger scheduled
#   bash deploy/scripts/backup.sh --trigger pre_upgrade    # 升级前额外备份
#   bash deploy/scripts/backup.sh --offsite local --keep-days 30
#   bash deploy/scripts/backup.sh --no-offsite --no-record # 仅本地调试
#
# 产出（默认 /var/lib/ventasync-backups，**不在 Git 工作目录内**）：
#   releases/<UTC时间>/
#     ventasync_center.sql.gz            中心库（身份/权限/配置/运维元数据）
#     vs_tenant_<id>.sql.gz              各租户业务库
#     config/.env                        必要配置（权限 600）
#     config/docker-compose*.yml
#     config/nginx.conf / Caddyfile
#     catalog_media.tar.gz               本地文件（商品图片卷，存在时）
#     manifest.json                      清单（数据库/配置/文件/校验摘要）
#     bundle.tar.gz.enc                  加密归档（AES-256-CBC/PBKDF2，异地传输单元）
#     bundle.tar.gz.enc.sha256
#
# 约定：
#   * 目录 700、文件 600；备份根目录可用 BACKUP_ROOT 覆盖，已加入 .gitignore；
#   * 保留 30 天（BACKUP_RETENTION_DAYS），每日 03:30（租户服务器本地时区）由
#     systemd timer 触发，见 deploy/systemd/ventasync-backup.timer；
#   * 加密密钥文件 /etc/ventasync/backup.key（0600，首次自动生成），**不进入备份包**；
#   * 异地存储为独立私有目录/私有桶（BACKUP_OFFSITE_*），不复用商品图片公开目录；
#   * 生成 → 校验 → 加密 → 异地上传 → 记录中心库 backup_records（可用 --no-record 关闭）。
#
# 运行身份与权限（重要）：
#   Agent 执行进程用户是 erp-agent-exec（共享组 erp-agent），升级前备份也以该用户运行，
#   因此备份根目录与密钥目录必须由管理员预先初始化（root 执行一次）：
#       sudo bash deploy/scripts/install_backup_dirs.sh
#   目录约定：/var/lib/ventasync-backups、/etc/ventasync 属组 erp-agent 2770，
#   密钥 /etc/ventasync/backup.key 640 root:erp-agent；tmpfiles 配置见
#   deploy/systemd/ventasync-backups.tmpfiles.conf（重启后保持权限）。
#   若脚本以非 root 运行且目录不可写，会给出明确修复命令并以退出码 1 结束（升级会因此中止，
#   这是预期：备份失败不得继续升级）。
#
# 退出码：0=成功；1=本地生成/校验失败或权限未初始化；2=异地加密上传失败（本地包仍保留）。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DEPLOY_DIR}/.." && pwd)"
COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.yml"

if [[ -f "${DEPLOY_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${DEPLOY_DIR}/.env"
    set +a
fi

# ---------------------------------------------------------------- 默认配置
: "${CENTER_DB_NAME:=ventasync_center}"
: "${BACKUP_ROOT:=/var/lib/ventasync-backups}"
: "${BACKUP_RETENTION_DAYS:=30}"
: "${BACKUP_TRIGGER:=scheduled}"
: "${BACKUP_INSTANCE_ID:=}"
: "${BACKUP_ENCRYPTION_KEY_FILE:=/etc/ventasync/backup.key}"
: "${BACKUP_OFFSITE_MODE:=none}"           # none | local | s3
: "${BACKUP_OFFSITE_DIR:=${BACKUP_ROOT}/offsite}"
: "${BACKUP_RECORD:=1}"
: "${BACKUP_SKIP_MEDIA:=0}"
: "${BACKUP_S3_BUCKET:=}"
: "${BACKUP_S3_ENDPOINT:=}"
: "${BACKUP_S3_REGION:=us-east-1}"
: "${BACKUP_S3_ACCESS_KEY:=}"
: "${BACKUP_S3_SECRET_KEY:=}"
: "${BACKUP_S3_PREFIX:=backups}"

TRIGGER="${BACKUP_TRIGGER}"
INSTANCE_ID="${BACKUP_INSTANCE_ID}"
KEEP_DAYS="${BACKUP_RETENTION_DAYS}"
OFFSITE_MODE="${BACKUP_OFFSITE_MODE}"
RECORD="${BACKUP_RECORD}"
SKIP_MEDIA="${BACKUP_SKIP_MEDIA}"
BACKUP_GROUP="${BACKUP_GROUP:-erp-agent}"
CENTER_ONLY=0
DRY_RUN=0

log() { printf '[backup] %s\n' "$*"; }
warn() { printf '[backup] 警告：%s\n' "$*" >&2; }
die() { printf '[backup] 错误：%s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

参数：
  --trigger <manual|scheduled|pre_upgrade>  触发来源（默认 scheduled）
  --instance-id <ID>                        实例ID（写入 backup_records；默认取 BACKUP_INSTANCE_ID）
  --offsite <none|local|s3>                 异地模式（默认 BACKUP_OFFSITE_MODE）
  --keep-days <天数>                        本地/异地镜像保留天数（默认 30）
  --skip-media                              跳过商品图片卷备份
  --center-only                             仅备份中心库（中心服务器上租户库在远端时使用）
  --no-offsite                              本次不上传异地
  --no-record                               不写中心库 backup_records
  --dry-run                                 只打印计划，不做任何写操作
  -h, --help                                显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --trigger) [[ $# -ge 2 ]] || die "--trigger 需要参数"; TRIGGER="$2"; shift 2 ;;
        --instance-id) [[ $# -ge 2 ]] || die "--instance-id 需要参数"; INSTANCE_ID="$2"; shift 2 ;;
        --offsite) [[ $# -ge 2 ]] || die "--offsite 需要参数"; OFFSITE_MODE="$2"; shift 2 ;;
        --keep-days) [[ $# -ge 2 ]] || die "--keep-days 需要参数"; KEEP_DAYS="$2"; shift 2 ;;
        --skip-media) SKIP_MEDIA=1; shift ;;
        --center-only) CENTER_ONLY=1; shift ;;
        --no-offsite) OFFSITE_MODE="none"; shift ;;
        --no-record) RECORD=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1（使用 -h 查看帮助）" ;;
    esac
done

case "${TRIGGER}" in manual|scheduled|pre_upgrade) ;; *) die "非法 --trigger：${TRIGGER}" ;; esac
case "${OFFSITE_MODE}" in none|local|s3) ;; *) die "非法 --offsite：${OFFSITE_MODE}" ;; esac
[[ "${KEEP_DAYS}" =~ ^[0-9]+$ ]] || die "--keep-days 必须是非负整数"
[[ -z "${INSTANCE_ID}" || "${INSTANCE_ID}" =~ ^[0-9]+$ ]] || die "--instance-id 必须是正整数"
[[ -f "${COMPOSE_FILE}" ]] || die "找不到 Compose 文件：${COMPOSE_FILE}"

umask 077
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${BACKUP_ROOT}/releases/${STAMP}"
CONFIG_DIR="${RUN_DIR}/config"
KEY_FILE="${BACKUP_ENCRYPTION_KEY_FILE}"

if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "dry-run：将备份到 ${RUN_DIR}（trigger=${TRIGGER}, offsite=${OFFSITE_MODE}, keep=${KEEP_DAYS}天）"
    exit 0
fi

# ---------------------------------------------------------------- 依赖检查
command -v docker >/dev/null 2>&1 || die "未找到 docker 命令"
command -v gzip >/dev/null 2>&1 || die "未找到 gzip 命令"
command -v openssl >/dev/null 2>&1 || die "未找到 openssl 命令（加密备份必需）"
command -v python3 >/dev/null 2>&1 || die "未找到 python3 命令（生成清单/调用记录接口必需）"
compose() { docker compose -f "${COMPOSE_FILE}" "$@"; }
compose ps --status running --services 2>/dev/null | grep -qx "mysql" \
    || die "mysql 服务未运行，请先启动：docker compose -f deploy/docker-compose.yml up -d mysql"

# ---------------------------------------------------------------- 目录与密钥权限
# Agent 执行用户为 erp-agent-exec（组 erp-agent）。管理员应预先执行 install_backup_dirs.sh；
# 这里再做一次防御性检查，避免以错误用户运行时静默失败。
ensure_writable_dir() {
    local dir="$1"
    if [[ ! -d "${dir}" ]]; then
        if [[ "$(id -u)" -eq 0 ]]; then
            install -d -m 2770 -o root -g "${BACKUP_GROUP}" "${dir}" 2>/dev/null \
                || install -d -m 700 "${dir}"
            log "已创建备份目录：${dir}（属组 ${BACKUP_GROUP:-root}，2770）"
        else
            die "备份目录不存在：${dir}。请管理员执行一次：sudo bash deploy/scripts/install_backup_dirs.sh"
        fi
    fi
    if [[ ! -w "${dir}" ]]; then
        die "备份目录不可写：${dir}（当前用户 $(id -un)）。请管理员执行：sudo bash deploy/scripts/install_backup_dirs.sh（属组 ${BACKUP_GROUP}，权限 2770）"
    fi
}
KEY_DIR="$(dirname "${KEY_FILE}")"
ensure_writable_dir "${BACKUP_ROOT}"
ensure_writable_dir "${BACKUP_ROOT}/releases"

# 加密密钥：内容不进备份包；丢失将无法解密异地包。管理员初始化时生成 640 root:erp-agent；
# 若缺失且当前用户可写密钥目录（root 或已授权属组），则生成 600/640。
if [[ ! -s "${KEY_FILE}" ]]; then
    ensure_writable_dir "${KEY_DIR}"
    ( umask 027; openssl rand -base64 32 > "${KEY_FILE}" )
    chmod 640 "${KEY_FILE}" 2>/dev/null || chmod 600 "${KEY_FILE}"
    if [[ "$(id -u)" -eq 0 ]] && getent group "${BACKUP_GROUP}" >/dev/null 2>&1; then
        chown "root:${BACKUP_GROUP}" "${KEY_FILE}" 2>/dev/null || true
    fi
    log "已生成备份加密密钥：${KEY_FILE}（请异地妥善保管，丢失无法恢复异地备份）"
fi
[[ -r "${KEY_FILE}" ]] || die "备份加密密钥不可读：${KEY_FILE}（应为 640 root:${BACKUP_GROUP} 或 600）"

install -d -m 700 "${RUN_DIR}" "${CONFIG_DIR}"

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        printf 'unavailable'
    fi
}

# ---------------------------------------------------------------- 数据库
mysql_exec() {
    compose exec -T -e CENTER_DB="${CENTER_DB_NAME}" mysql sh -c '
        set -eu
        export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD 未设置}"
        exec mysql -N -B -uroot "$@"
    ' sh "$@"
}

dump_one() {
    local db="$1"
    local file="${RUN_DIR}/${db}.sql.gz"
    local tmp="${file}.part"
    local size tables digest
    log "备份数据库 ${db}"
    if ! compose exec -T -e DUMP_DB="${db}" mysql sh -c '
        set -eu
        : "${DUMP_DB:?DUMP_DB 未设置}"
        export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD 未设置}"
        exec mysqldump -uroot --single-transaction --quick --hex-blob --routines --triggers \
            --default-character-set=utf8mb4 --set-gtid-purged=OFF --no-tablespaces --databases "${DUMP_DB}"
    ' | gzip -c > "${tmp}"; then
        rm -f "${tmp}"
        die "mysqldump 失败：${db}"
    fi
    mv "${tmp}" "${file}"
    gzip -t "${file}" || die "备份文件 gzip 校验失败：${db}"
    size="$(wc -c < "${file}" | tr -d ' ')"
    [[ "${size}" -gt 0 ]] || die "备份文件为空：${db}"
    digest="$(sha256_of "${file}")"
    if [[ "${digest}" != "unavailable" ]]; then
        printf '%s  %s\n' "${digest}" "$(basename "${file}")" > "${file}.sha256"
    fi
    tables="$(gzip -dc "${file}" | grep -c 'CREATE TABLE' || true)"
    log "完成 ${db}：表 ${tables} 个，大小 ${size} 字节"
    echo "${tables}" >> "${RUN_DIR}/.table_counts"
}

CENTER_TABLES="$(mysql_exec -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${CENTER_DB_NAME}'" | tr -d ' ' || echo 0)"
[[ "${CENTER_TABLES}" -gt 0 ]] || die "中心库 ${CENTER_DB_NAME} 不存在或为空"

dump_one "${CENTER_DB_NAME}"
if [[ "${CENTER_ONLY}" -eq 1 ]]; then
    log "按参数仅备份中心库（--center-only），跳过租户库"
else
    # 只备份本机存在的租户库：中心服务器上租户库可能在远端独立实例，直接 dump 会失败
    LOCAL_DBS="$(mysql_exec -e "SELECT schema_name FROM information_schema.schemata" || true)"
    TENANT_DBS="$(mysql_exec -e "SELECT database_name FROM ${CENTER_DB_NAME}.tenant_databases WHERE database_name IS NOT NULL AND LENGTH(database_name) > 0 ORDER BY tenant_id" || true)"
    : > "${RUN_DIR}/.skipped_dbs"
    for tenant_db in ${TENANT_DBS}; do
        [[ "${tenant_db}" =~ ^[A-Za-z0-9_]+$ ]] || die "非法租户库名：${tenant_db}"
        if ! grep -Fxq "${tenant_db}" <<< "${LOCAL_DBS}"; then
            warn "租户库 ${tenant_db} 不在本机 MySQL（远端实例），跳过导出；请在该租户实例上单独备份"
            printf '%s\n' "${tenant_db}" >> "${RUN_DIR}/.skipped_dbs"
            continue
        fi
        dump_one "${tenant_db}"
    done
fi
CENTER_DB_TABLES="$(sed -n '1p' "${RUN_DIR}/.table_counts" 2>/dev/null | tr -d ' ' || echo 0)"
rm -f "${RUN_DIR}/.table_counts"

# ---------------------------------------------------------------- 配置与本地文件
copy_config() {
    local src="$1" dest="$2"
    [[ -f "${src}" ]] || return 0
    mkdir -p "$(dirname "${CONFIG_DIR}/${dest}")"
    cp -p "${src}" "${CONFIG_DIR}/${dest}"
    chmod 600 "${CONFIG_DIR}/${dest}"
    log "备份配置 ${dest}"
}
copy_config "${DEPLOY_DIR}/.env" ".env"
copy_config "${DEPLOY_DIR}/docker-compose.yml" "docker-compose.yml"
copy_config "${DEPLOY_DIR}/docker-compose.server.yml" "docker-compose.server.yml"
copy_config "${DEPLOY_DIR}/nginx/nginx.conf" "nginx.conf"
for caddy_file in Caddyfile Caddyfile.example; do
    copy_config "${DEPLOY_DIR}/caddy/${caddy_file}" "caddy/${caddy_file}"
done

dump_media() {
    [[ "${SKIP_MEDIA}" -eq 1 ]] && { log "按参数跳过商品图片卷"; return 0; }
    local api_id file="${RUN_DIR}/catalog_media.tar.gz" tmp="${RUN_DIR}/catalog_media.tar.gz.part"
    api_id="$(compose ps -a -q api 2>/dev/null || true)"
    [[ -n "${api_id}" ]] || { warn "api 容器不存在，跳过商品图片卷"; return 0; }
    if ! docker inspect --format '{{range .Mounts}}{{println .Destination}}{{end}}' "${api_id}" | grep -Fxq '/app/var/catalog-media'; then
        log "未配置商品图片卷，跳过本地文件备份"
        return 0
    fi
    log "备份商品图片卷"
    if ! docker cp "${api_id}:/app/var/catalog-media/." - 2>/dev/null | gzip > "${tmp}"; then
        rm -f "${tmp}"
        warn "商品图片卷备份失败（不阻塞数据库备份）"
        return 0
    fi
    gzip -t "${tmp}" || { rm -f "${tmp}"; warn "商品图片卷 gzip 校验失败"; return 0; }
    mv "${tmp}" "${file}"
    chmod 600 "${file}"
    sha256_of "${file}" > "${file}.sha256" 2>/dev/null || true
}
dump_media

# ---------------------------------------------------------------- 清单与本地校验
log "生成本地清单与校验"
python3 - "${RUN_DIR}" "${CENTER_DB_NAME}" "${TRIGGER}" "${INSTANCE_ID}" "${KEEP_DAYS}" <<'PY'
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

run_dir = Path(sys.argv[1])
center_db = sys.argv[2]
trigger = sys.argv[3]
instance_id = sys.argv[4]
retention_days = int(sys.argv[5] or 30)

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

files = []
for path in sorted(run_dir.rglob("*")):
    if not path.is_file() or path.name == "manifest.json":
        continue
    files.append(
        {
            "path": str(path.relative_to(run_dir)),
            "size": path.stat().st_size,
            "sha256": sha256(path),
        }
    )

sql_files = [item for item in files if item["path"].endswith(".sql.gz")]
config_files = [item for item in files if item["path"].startswith("config/")]
media_files = [item for item in files if item["path"].startswith("catalog_media")]
skipped_path = run_dir / ".skipped_dbs"
skipped_dbs = []
if skipped_path.exists():
    skipped_dbs = [line.strip() for line in skipped_path.read_text(encoding="utf-8").splitlines() if line.strip()]
manifest = {
    "run_id": run_dir.name,
    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "trigger": trigger,
    "instance_id": int(instance_id) if instance_id else None,
    "center_database": center_db,
    "databases": [Path(item["path"]).name.replace(".sql.gz", "") for item in sql_files],
    "skipped_remote_databases": skipped_dbs,
    "config": [item["path"] for item in config_files],
    "files": files,
    "local": {
        "status": "generated",
        "dir": str(run_dir),
        "size_bytes": sum(item["size"] for item in files),
    },
    "checks": {
        "sha256_ok": bool(files) and all(len(item["sha256"]) == 64 for item in files),
        "center_dump_present": any(center_db in item["path"] for item in sql_files),
        "config_present": bool(config_files),
        "media_present": bool(media_files),
        "sql_file_count": len(sql_files),
    },
    "encryption": {"algorithm": "aes-256-cbc/pbkdf2", "key_file": "BACKUP_ENCRYPTION_KEY_FILE"},
    "retention_days": retention_days,
    "hostname": os.uname().nodename if hasattr(os, "uname") else "",
}
(run_dir / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
os.chmod(run_dir / "manifest.json", 0o600)
if skipped_path.exists():
    skipped_path.unlink()
# 本地校验结果（写回清单，供后端记录与演练核对）
checks = manifest["checks"]
checks["local_verified"] = bool(
    checks["sha256_ok"] and checks["center_dump_present"] and checks["config_present"]
)
manifest["local"]["status"] = "verified" if checks["local_verified"] else "failed"
(run_dir / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps({"verified": checks["local_verified"], "sql_files": checks["sql_file_count"]}))
if not checks["local_verified"]:
    sys.exit(1)
PY
[[ "${CENTER_DB_TABLES}" -gt 0 ]] || die "中心库备份未包含表结构"

# ---------------------------------------------------------------- 打包加密
log "打包并加密（AES-256-CBC / PBKDF2）"
ENC_FILE="${RUN_DIR}/bundle.tar.gz.enc"
PLAIN_TMP="${RUN_DIR}/.bundle.tar.gz"
tar -C "${RUN_DIR}" -czf "${PLAIN_TMP}" --exclude='bundle.tar.gz.enc*' .
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
    -in "${PLAIN_TMP}" -out "${ENC_FILE}" -pass "file:${KEY_FILE}"
rm -f "${PLAIN_TMP}"
chmod 600 "${ENC_FILE}"
ENC_SHA="$(sha256_of "${ENC_FILE}")"
printf '%s  %s\n' "${ENC_SHA}" "$(basename "${ENC_FILE}")" > "${ENC_FILE}.sha256"
chmod 600 "${ENC_FILE}.sha256"
# 解密回读校验：确保密钥可用、包未损坏
if ! openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in "${ENC_FILE}" -pass "file:${KEY_FILE}" | tar -tzf - >/dev/null 2>&1; then
    die "加密包回读校验失败（密钥或归档损坏）"
fi

# ---------------------------------------------------------------- 异地上传
OFFSITE_STATUS="skipped"
OFFSITE_KEY=""
OFFSITE_BUCKET=""
OFFSITE_ERROR=""
if [[ "${OFFSITE_MODE}" == "local" ]]; then
    log "上传异地私有目录：${BACKUP_OFFSITE_DIR}"
    OFF_DEST="${BACKUP_OFFSITE_DIR}/${INSTANCE_ID:-instance-0}/${STAMP}"
    install -d -m 700 "${BACKUP_OFFSITE_DIR}" "${OFF_DEST}"
    cp -p "${ENC_FILE}" "${OFF_DEST}/"
    cp -p "${ENC_FILE}.sha256" "${OFF_DEST}/"
    cp -p "${RUN_DIR}/manifest.json" "${OFF_DEST}/"
    chmod 600 "${OFF_DEST}"/*
    if [[ "$(sha256_of "${OFF_DEST}/$(basename "${ENC_FILE}")")" == "${ENC_SHA}" ]]; then
        OFFSITE_STATUS="uploaded"
        OFFSITE_KEY="file:${OFF_DEST}/$(basename "${ENC_FILE}")"
        log "异地私有目录上传并校验通过：${OFFSITE_KEY}"
    else
        OFFSITE_STATUS="failed"
        OFFSITE_ERROR="异地目录校验值不一致"
        warn "${OFFSITE_ERROR}"
    fi
elif [[ "${OFFSITE_MODE}" == "s3" ]]; then
    [[ -n "${BACKUP_S3_BUCKET}" ]] || die "未配置 BACKUP_S3_BUCKET（异地 S3 备份桶）"
    log "上传异地私有存储桶：${BACKUP_S3_BUCKET}（前缀 ${BACKUP_S3_PREFIX}）"
    API_ID="$(compose ps -q api 2>/dev/null || true)"
    [[ -n "${API_ID}" ]] || die "api 容器未运行，无法执行 S3 上传"
    TMP_IN_CONTAINER="/tmp/erp-backup-${STAMP}.enc"
    docker cp "${ENC_FILE}" "${API_ID}:${TMP_IN_CONTAINER}" >/dev/null
    S3_KEY="${BACKUP_S3_PREFIX}/${INSTANCE_ID:-instance-0}/${STAMP}/$(basename "${ENC_FILE}")"
    set +e
    S3_OUTPUT="$(compose exec -T \
        -e BACKUP_FILE="${TMP_IN_CONTAINER}" \
        -e BACKUP_S3_BUCKET="${BACKUP_S3_BUCKET}" \
        -e BACKUP_S3_KEY="${S3_KEY}" \
        -e BACKUP_S3_ENDPOINT="${BACKUP_S3_ENDPOINT}" \
        -e BACKUP_S3_REGION="${BACKUP_S3_REGION}" \
        -e BACKUP_S3_ACCESS_KEY="${BACKUP_S3_ACCESS_KEY}" \
        -e BACKUP_S3_SECRET_KEY="${BACKUP_S3_SECRET_KEY}" \
        -e BACKUP_SHA256="${ENC_SHA}" \
        api python - <<'PY'
import hashlib
import json
import os
import sys

try:
    import boto3
    from botocore.config import Config
except ImportError:  # pragma: no cover - 镜像内已包含 boto3
    print(json.dumps({"ok": False, "error": "boto3 不可用"}))
    sys.exit(0)

path = os.environ["BACKUP_FILE"]
bucket = os.environ["BACKUP_S3_BUCKET"]
key = os.environ["BACKUP_S3_KEY"]
client = boto3.client(
    "s3",
    endpoint_url=os.environ.get("BACKUP_S3_ENDPOINT") or None,
    region_name=os.environ.get("BACKUP_S3_REGION") or "us-east-1",
    aws_access_key_id=os.environ.get("BACKUP_S3_ACCESS_KEY") or None,
    aws_secret_access_key=os.environ.get("BACKUP_S3_SECRET_KEY") or None,
    config=Config(s3={"addressing_style": "path"}),
)
try:
    with open(path, "rb") as handle:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=handle,
            ContentType="application/octet-stream",
            Metadata={"sha256": os.environ.get("BACKUP_SHA256", "")},
        )
    head = client.head_object(Bucket=bucket, Key=key)
    print(json.dumps({"ok": True, "bucket": bucket, "key": key, "size": int(head["ContentLength"])}))
except Exception as exc:  # noqa: BLE001
    print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
PY
)"
    S3_CODE=$?
    set -e
    rm -f "${TMP_IN_CONTAINER}" >/dev/null 2>&1 || true
    docker exec "${API_ID}" rm -f "${TMP_IN_CONTAINER}" >/dev/null 2>&1 || true
    if [[ "${S3_CODE}" -ne 0 ]]; then
        OFFSITE_STATUS="failed"
        OFFSITE_ERROR="S3 上传命令失败"
        warn "${OFFSITE_ERROR}：${S3_OUTPUT:0:300}"
    else
        PARSED_OK="$(printf '%s' "${S3_OUTPUT}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ok"))' 2>/dev/null || echo False)"
        if [[ "${PARSED_OK}" == "True" ]]; then
            OFFSITE_STATUS="uploaded"
            OFFSITE_KEY="s3://${BACKUP_S3_BUCKET}/${S3_KEY}"
            OFFSITE_BUCKET="${BACKUP_S3_BUCKET}"
            log "异地私有存储上传成功：${OFFSITE_KEY}"
        else
            OFFSITE_STATUS="failed"
            OFFSITE_ERROR="S3 上传返回失败"
            warn "${OFFSITE_ERROR}：${S3_OUTPUT:0:300}"
        fi
    fi
else
    log "未配置异地存储（BACKUP_OFFSITE_MODE=none），本地备份完成"
fi

# ---------------------------------------------------------------- 回写清单与本地权限
python3 - "${RUN_DIR}" "${OFFSITE_STATUS}" "${OFFSITE_KEY}" "${OFFSITE_BUCKET}" "${OFFSITE_ERROR}" "${ENC_SHA}" "${ENC_FILE}" <<'PY'
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

run_dir = Path(sys.argv[1])
status, key, bucket, error, checksum, enc_name = sys.argv[2:8]
manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
manifest["offsite"] = {
    "status": status,
    "key": key,
    "bucket": bucket,
    "error": error,
    "checksum": f"sha256:{checksum}",
    "size_bytes": (run_dir / Path(enc_name).name).stat().st_size,
}
manifest["files"].append(
    {
        "path": Path(enc_name).name,
        "size": (run_dir / Path(enc_name).name).stat().st_size,
        "sha256": checksum,
    }
)
(run_dir / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
os.chmod(run_dir / "manifest.json", 0o600)
PY
# 目录 700 / 文件 600（含子目录）
find "${RUN_DIR}" -type d -exec chmod 700 {} +
find "${RUN_DIR}" -type f -exec chmod 600 {} +

# ---------------------------------------------------------------- 记录中心库
RECORD_STATUS=0
if [[ "${RECORD}" -eq 1 && -n "${INSTANCE_ID}" ]]; then
    log "写入中心库 backup_records（instance=${INSTANCE_ID}）"
    RECORD_JSON="$(python3 - "${RUN_DIR}/manifest.json" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
payload["files"] = payload.get("files", [])[:200]  # 只保留清单摘要，避免环境变量过大
print(json.dumps(payload, ensure_ascii=False))
PY
)"
    set +e
    RECORD_OUTPUT="$(compose exec -T -e BACKUP_MANIFEST_JSON="${RECORD_JSON}" \
        api python -m app.modules.ops.backup_cli apply 2>&1)"
    RECORD_CODE=$?
    set -e
    if [[ "${RECORD_CODE}" -eq 0 ]]; then
        log "备份记录已写入中心库"
    else
        RECORD_STATUS=1
        warn "写入中心库备份记录失败（备份本身有效，可事后补录）：${RECORD_OUTPUT:0:300}"
    fi
elif [[ "${RECORD}" -eq 1 ]]; then
    warn "未设置 BACKUP_INSTANCE_ID / --instance-id，跳过中心库备份记录"
fi

# ---------------------------------------------------------------- 清理过期
if [[ "${KEEP_DAYS}" -gt 0 ]]; then
    log "清理 ${BACKUP_ROOT}/releases 中超过 ${KEEP_DAYS} 天的本地备份"
    find "${BACKUP_ROOT}/releases" -mindepth 1 -maxdepth 1 -type d -mtime "+${KEEP_DAYS}" -exec rm -rf {} + || true
    if [[ -d "${BACKUP_OFFSITE_DIR}" ]]; then
        log "清理 ${BACKUP_OFFSITE_DIR} 中超过 ${KEEP_DAYS} 天的异地镜像"
        find "${BACKUP_OFFSITE_DIR}" -mindepth 2 -maxdepth 2 -type d -mtime "+${KEEP_DAYS}" -exec rm -rf {} + || true
    fi
fi

# ---------------------------------------------------------------- 汇总
RUN_SIZE="$(du -sb "${RUN_DIR}" | awk '{print $1}')"
ENC_SIZE="$(wc -c < "${ENC_FILE}" | tr -d ' ')"
cat <<EOF

BACKUP_KEY=${RUN_DIR}
BACKUP_SIZE=${RUN_SIZE}
BACKUP_DIR=${RUN_DIR}
BACKUP_ENCRYPTED=${ENC_FILE}
BACKUP_ENCRYPTED_SIZE=${ENC_SIZE}
BACKUP_CHECKSUM=sha256:${ENC_SHA}
BACKUP_TRIGGER=${TRIGGER}
BACKUP_OFFSITE_STATUS=${OFFSITE_STATUS}
BACKUP_OFFSITE_KEY=${OFFSITE_KEY}
BACKUP_RECORDED=$([[ "${RECORD_STATUS}" -eq 0 && "${RECORD}" -eq 1 ]] && echo 1 || echo 0)
EOF

if [[ "${OFFSITE_STATUS}" == "failed" ]]; then
    warn "异地加密上传失败：本地备份保留在 ${RUN_DIR}"
    exit 2
fi
log "备份完成：${RUN_DIR}"
log "恢复演练：bash deploy/scripts/restore_drill.sh --bundle ${ENC_FILE} --latest-record"
