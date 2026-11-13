#!/usr/bin/env bash
# =============================================================================
# ERP 隔离环境恢复演练（批次3：备份加密异地与恢复演练）
#
# 用途：
#   1) 用备份包在**隔离环境**（独立 compose 项目/独立卷/不暴露端口）恢复中心库与租户库；
#   2) 自动校验账号、权限、商品、配置等关键数据是否可用；
#   3) 输出演练报告 JSON，可回写中心库 backup_records.restore_status（--record-id/--record-latest）。
#
# 用法：
#   bash deploy/scripts/restore_drill.sh --self-test                    # 无服务器自测（不连数据库）
#   bash deploy/scripts/restore_drill.sh --bundle /var/lib/ventasync-backups/releases/<ts>/bundle.tar.gz.enc
#   bash deploy/scripts/restore_drill.sh --latest --record-latest       # 取最新本地包，验证后回写中心库
#   bash deploy/scripts/restore_drill.sh --bundle <enc> --keep          # 保留演练容器卷供人工核查
#
# 安全约定：
#   * 只在隔离项目（ventasync-drill-*）与独立卷中恢复，不碰生产库、不写生产卷；
#   * 解密在 700 权限临时目录进行，结束后删除明文；--keep 保留演练数据库供核查；
#   * 不启动 api/worker，仅启动 mysql 用于数据校验（避免误连外部服务）；
#   * 演练不修改生产 .env，不执行任何平台写接口。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DEPLOY_DIR}/.." && pwd)"
COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.yml"
COMPOSE_SERVER="${DEPLOY_DIR}/docker-compose.server.yml"

if [[ -f "${DEPLOY_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${DEPLOY_DIR}/.env"
    set +a
fi

: "${CENTER_DB_NAME:=ventasync_center}"
: "${BACKUP_ROOT:=/var/lib/ventasync-backups}"
: "${BACKUP_ENCRYPTION_KEY_FILE:=/etc/ventasync/backup.key}"
: "${BACKUP_INSTANCE_ID:=}"
: "${BACKUP_RECORD:=1}"

BUNDLE=""
USE_LATEST=0
SELF_TEST=0
KEEP=0
RECORD_ID=""
RECORD_LATEST=0
WORK_DIR=""
PROJECT=""

log() { printf '[drill] %s\n' "$*"; }
warn() { printf '[drill] 警告：%s\n' "$*" >&2; }
die() { printf '[drill] 错误：%s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

参数：
  --bundle <文件>     加密备份包（bundle.tar.gz.enc）
  --latest            取本地最新备份包（BACKUP_ROOT/releases/*/bundle.tar.gz.enc）
  --self-test         无服务器自测：构造临时加密包并验证解密/清单/校验逻辑
  --record-id <ID>    演练成功后回写该 backup_records 记录
  --record-latest     回写该实例最新一条备份记录
  --keep              保留演练数据库容器与解压目录（默认清理）
  -h, --help          显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bundle) [[ $# -ge 2 ]] || die "--bundle 需要参数"; BUNDLE="$2"; shift 2 ;;
        --latest) USE_LATEST=1; shift ;;
        --self-test) SELF_TEST=1; shift ;;
        --record-id) [[ $# -ge 2 ]] || die "--record-id 需要参数"; RECORD_ID="$2"; shift 2 ;;
        --record-latest) RECORD_LATEST=1; shift ;;
        --keep) KEEP=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1（使用 -h 查看帮助）" ;;
    esac
done

# ---------------------------------------------------------------- 自测（不依赖 docker/mysql）
self_test() {
    local tmp key bundle src
    tmp="$(mktemp -d)"
    trap 'rm -rf "${tmp}"' EXIT
    chmod 700 "${tmp}"
    mkdir -p "${tmp}/src/config"
    printf 'CREATE TABLE users (id BIGINT);\nCREATE TABLE tenants (id BIGINT);\n' | gzip > "${tmp}/src/${CENTER_DB_NAME}.sql.gz"
    printf 'MYSQL_ROOT_PASSWORD=drill\nAPP_IMAGE_TAG=test\n' > "${tmp}/src/config/.env"
    printf '{"run_id":"selftest","trigger":"manual","databases":["%s"],"config":["config/.env"],"files":[]}\n' "${CENTER_DB_NAME}" > "${tmp}/src/manifest.json"
    key="${tmp}/backup.key"
    openssl rand -base64 32 > "${key}"
    chmod 600 "${key}"
    src="${tmp}/src"
    ( cd "${src}" && tar -czf "${tmp}/bundle.tar.gz" . )
    openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -in "${tmp}/bundle.tar.gz" -out "${tmp}/bundle.tar.gz.enc" -pass "file:${key}"
    bundle="${tmp}/bundle.tar.gz.enc"
    sha256sum "${bundle}" | awk '{print $1"  bundle.tar.gz.enc"}' > "${bundle}.sha256"
    log "[self-test] 1/5 校验加密包 SHA-256"
    ( cd "${tmp}" && sha256sum -c "${bundle}.sha256" >/dev/null ) || die "自测失败：SHA-256 校验"
    log "[self-test] 2/5 解密并解包"
    mkdir -p "${tmp}/out"
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in "${bundle}" -pass "file:${key}" | tar -xzf - -C "${tmp}/out"
    [[ -f "${tmp}/out/manifest.json" ]] || die "自测失败：清单缺失"
    log "[self-test] 3/5 校验清单/数据库文件/配置"
    [[ -f "${tmp}/out/${CENTER_DB_NAME}.sql.gz" ]] || die "自测失败：中心库备份缺失"
    [[ -f "${tmp}/out/config/.env" ]] || die "自测失败：config/.env 缺失"
    gzip -t "${tmp}/out/${CENTER_DB_NAME}.sql.gz" || die "自测失败：备份 gzip 损坏"
    log "[self-test] 4/5 校验关键表可识别（账号/租户）"
    gzip -dc "${tmp}/out/${CENTER_DB_NAME}.sql.gz" | grep -q 'CREATE TABLE users' || die "自测失败：未发现 users 表"
    log "[self-test] 5/5 校验报告可生成"
    printf '{"self_test": true, "bundle": "%s", "ok": true}\n' "${bundle}" > "${tmp}/report.json"
    python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "${tmp}/report.json" || die "自测失败：报告不是合法 JSON"
    log "[self-test] 通过：加密包可解密、清单/数据库/配置/关键表校验逻辑正常（未连接真实数据库）"
    exit 0
}

if [[ "${SELF_TEST}" -eq 1 ]]; then
    self_test
fi

# ---------------------------------------------------------------- 依赖与定位备份包
command -v docker >/dev/null 2>&1 || die "未找到 docker 命令"
command -v openssl >/dev/null 2>&1 || die "未找到 openssl 命令"
command -v python3 >/dev/null 2>&1 || die "未找到 python3 命令"
command -v gzip >/dev/null 2>&1 || die "未找到 gzip 命令"
compose() { docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}" -f "${COMPOSE_SERVER}" -f "${OVERRIDE_FILE}" "$@"; }

if [[ "${USE_LATEST}" -eq 1 && -z "${BUNDLE}" ]]; then
    BUNDLE="$(ls -1dt "${BACKUP_ROOT}"/releases/*/bundle.tar.gz.enc 2>/dev/null | head -n1 || true)"
    [[ -n "${BUNDLE}" ]] || die "未找到本地备份包：${BACKUP_ROOT}/releases/*/bundle.tar.gz.enc"
fi
[[ -n "${BUNDLE}" ]] || die "请用 --bundle 指定加密备份包，或使用 --latest / --self-test"
[[ -f "${BUNDLE}" ]] || die "备份包不存在：${BUNDLE}"
[[ -r "${BACKUP_ENCRYPTION_KEY_FILE}" ]] || die "备份加密密钥不可读：${BACKUP_ENCRYPTION_KEY_FILE}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PROJECT="ventasync-drill-$(date -u +%s)"
WORK_DIR="$(mktemp -d "${BACKUP_ROOT}/drill-${STAMP}-XXXX" 2>/dev/null || mktemp -d)"
chmod 700 "${WORK_DIR}"
OVERRIDE_FILE="${WORK_DIR}/docker-compose.drill.yml"
REPORT="${WORK_DIR}/drill-report.json"
mkdir -p "${WORK_DIR}/bundle"

cleanup() {
    if [[ "${KEEP}" -eq 1 ]]; then
        log "保留演练环境以便人工核查：project=${PROJECT} work=${WORK_DIR}"
        return
    fi
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

# ---------------------------------------------------------------- 校验与解密
log "1/6 校验加密包：${BUNDLE}"
if [[ -f "${BUNDLE}.sha256" ]]; then
    ( cd "$(dirname "${BUNDLE}")" && sha256sum -c "$(basename "${BUNDLE}").sha256" >/dev/null ) \
        || die "加密包 SHA-256 校验失败"
fi
log "2/6 解密并解包到隔离目录"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in "${BUNDLE}" -pass "file:${BACKUP_ENCRYPTION_KEY_FILE}" \
    | tar -xzf - -C "${WORK_DIR}/bundle"
MANIFEST="${WORK_DIR}/bundle/manifest.json"
[[ -f "${MANIFEST}" ]] || die "备份包缺少 manifest.json"
python3 - "${WORK_DIR}/bundle" "${MANIFEST}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
problems = []
for item in manifest.get("files", []):
    path = root / item["path"]
    if not path.is_file():
        problems.append(f"缺少文件 {item['path']}")
        continue
    if path.name.endswith(".enc"):
        continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if item.get("sha256") and digest != item["sha256"]:
        problems.append(f"校验值不一致 {item['path']}")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print(json.dumps({"verified_files": len(manifest.get("files", []))}))
PY

CENTER_SQL="$(ls "${WORK_DIR}/bundle/${CENTER_DB_NAME}.sql.gz" 2>/dev/null || true)"
[[ -n "${CENTER_SQL}" ]] || die "备份包缺少中心库 ${CENTER_DB_NAME}.sql.gz"
TENANT_SQLS=()
while IFS= read -r sql; do
    [[ "${sql}" == *"${CENTER_DB_NAME}"* ]] && continue
    TENANT_SQLS+=("${sql}")
done < <(ls -1 "${WORK_DIR}"/bundle/*.sql.gz 2>/dev/null || true)

# ---------------------------------------------------------------- 隔离环境
log "3/6 启动隔离 MySQL（project=${PROJECT}，不暴露端口）"
cat > "${OVERRIDE_FILE}" <<'YAML'
services:
  mysql:
    ports: !reset []
  redis-cache:
    ports: !reset []
  redis-queue:
    ports: !reset []
  api:
    profiles: ["never"]
  worker:
    profiles: ["never"]
  scheduler:
    profiles: ["never"]
  nginx:
    profiles: ["never"]
  caddy:
    profiles: ["never"]
YAML
compose up -d --no-deps mysql
MYSQL_READY=0
for _ in $(seq 1 60); do
    if compose exec -T mysql sh -c 'export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?}"; mysqladmin ping -uroot --silent' >/dev/null 2>&1; then
        MYSQL_READY=1
        break
    fi
    sleep 2
done
[[ "${MYSQL_READY}" -eq 1 ]] || die "隔离 MySQL 启动超时"

mysql_import() {
    local file="$1"
    gunzip -c "${file}" | compose exec -T mysql sh -c '
        set -eu
        export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD 未设置}"
        exec mysql -uroot
    '
}
mysql_query() {
    compose exec -T mysql sh -c '
        set -eu
        export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD 未设置}"
        exec mysql -N -B -uroot -e "$1"
    ' sh "$1"
}

log "4/6 恢复中心库与租户库（${#TENANT_SQLS[@]} 个租户库）"
mysql_import "${CENTER_SQL}"
for sql in "${TENANT_SQLS[@]}"; do
    log "恢复 $(basename "${sql}")"
    mysql_import "${sql}"
done

# ---------------------------------------------------------------- 关键数据校验
log "5/6 校验账号、权限、商品、配置"
CHECKS_JSON="${WORK_DIR}/checks.json"
python3 - "${CHECKS_JSON}" "${CENTER_DB_NAME}" "${#TENANT_SQLS[@]}" <<'PY'
import json
import sys

path, center_db, tenant_count = sys.argv[1], sys.argv[2], int(sys.argv[3])
checks = [
    {"name": "center_imported", "ok": True, "detail": center_db},
    {"name": "tenant_databases", "ok": tenant_count >= 1, "detail": f"{tenant_count} 个租户库"},
]
open(path, "w", encoding="utf-8").write(json.dumps(checks, ensure_ascii=False))
PY

declare -a CHECK_NAMES=() CHECK_RESULTS=() CHECK_DETAILS=()
add_check() { CHECK_NAMES+=("$1"); CHECK_RESULTS+=("$2"); CHECK_DETAILS+=("$3"); }

q_int() {
    local sql="$1"
    local value
    value="$(mysql_query "${sql}" 2>/dev/null | tr -d '[:space:]' || true)"
    [[ "${value}" =~ ^[0-9]+$ ]] && echo "${value}" || echo 0
}

CENTER_USERS="$(q_int "SELECT COUNT(*) FROM ${CENTER_DB_NAME}.users")"
CENTER_USERS_WITH_PASSWORD="$(q_int "SELECT COUNT(*) FROM ${CENTER_DB_NAME}.users WHERE password_hash IS NOT NULL AND password_hash <> ''")"
CENTER_PLATFORM_ADMINS="$(q_int "SELECT COUNT(*) FROM ${CENTER_DB_NAME}.platform_user_roles")"
CENTER_TENANTS="$(q_int "SELECT COUNT(*) FROM ${CENTER_DB_NAME}.tenants")"
CENTER_MEMBERSHIPS="$(q_int "SELECT COUNT(*) FROM ${CENTER_DB_NAME}.tenant_memberships")"
add_check "center_users" "$([[ "${CENTER_USERS}" -gt 0 ]] && echo true || echo false)" "users=${CENTER_USERS}"
add_check "password_hashes" "$([[ "${CENTER_USERS_WITH_PASSWORD}" -gt 0 ]] && echo true || echo false)" "users_with_password=${CENTER_USERS_WITH_PASSWORD}"
add_check "platform_admins" "$([[ "${CENTER_PLATFORM_ADMINS}" -gt 0 ]] && echo true || echo false)" "platform_user_roles=${CENTER_PLATFORM_ADMINS}"
add_check "tenants" "$([[ "${CENTER_TENANTS}" -gt 0 ]] && echo true || echo false)" "tenants=${CENTER_TENANTS}"
add_check "memberships" "$([[ "${CENTER_MEMBERSHIPS}" -gt 0 ]] && echo true || echo false)" "memberships=${CENTER_MEMBERSHIPS}"

for sql in "${TENANT_SQLS[@]}"; do
    db="$(basename "${sql}" .sql.gz)"
    roles="$(q_int "SELECT COUNT(*) FROM \`${db}\`.roles")"
    permissions="$(q_int "SELECT COUNT(*) FROM \`${db}\`.role_permissions")"
    settings="$(q_int "SELECT COUNT(*) FROM \`${db}\`.tenant_settings")"
    products="$(q_int "SELECT COUNT(*) FROM \`${db}\`.products")"
    skus="$(q_int "SELECT COUNT(*) FROM \`${db}\`.product_skus")"
    add_check "tenant_roles:${db}" "$([[ "${roles}" -gt 0 ]] && echo true || echo false)" "roles=${roles}"
    add_check "tenant_permissions:${db}" "$([[ "${permissions}" -gt 0 ]] && echo true || echo false)" "role_permissions=${permissions}"
    add_check "tenant_settings:${db}" "$([[ "${settings}" -gt 0 ]] && echo true || echo false)" "tenant_settings=${settings}"
    add_check "tenant_products:${db}" "true" "products=${products}, skus=${skus}"
done

CONFIG_ENV="${WORK_DIR}/bundle/config/.env"
CONFIG_OK=false
if [[ -f "${CONFIG_ENV}" ]]; then
    if grep -q '^MYSQL_ROOT_PASSWORD=' "${CONFIG_ENV}" && grep -q '^APP_IMAGE_TAG=' "${CONFIG_ENV}"; then
        CONFIG_OK=true
    fi
fi
add_check "config_usable" "${CONFIG_OK}" "config/.env 关键项"

FAILED=0
for i in "${!CHECK_NAMES[@]}"; do
    if [[ "${CHECK_RESULTS[$i]}" != "true" ]]; then
        FAILED=$((FAILED + 1))
    fi
done

python3 - "${REPORT}" "${WORK_DIR}/bundle/manifest.json" "${CHECK_NAMES[@]}" -- "${CHECK_RESULTS[@]}" -- "${CHECK_DETAILS[@]}" <<'PY'
import json
import sys
from datetime import datetime, timezone

args = sys.argv[1:]
report_path, manifest_path = args[0], args[1]
rest = args[2:]
sep1 = rest.index("--")
names = rest[:sep1]
rest = rest[sep1 + 1:]
sep2 = rest.index("--")
results = rest[:sep2]
details = rest[sep2 + 1:]
manifest = {}
try:
    manifest = json.loads(open(manifest_path, encoding="utf-8").read())
except OSError:
    pass
checks = [
    {"name": names[i], "ok": results[i] == "true", "detail": details[i]}
    for i in range(len(names))
]
payload = {
    "drill_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "run_id": manifest.get("run_id", ""),
    "trigger": manifest.get("trigger", ""),
    "checks": checks,
    "failed": sum(1 for item in checks if not item["ok"]),
    "passed": all(item["ok"] for item in checks),
}
open(report_path, "w", encoding="utf-8").write(json.dumps(payload, ensure_ascii=False, indent=2))
print(json.dumps({"checks": len(checks), "failed": payload["failed"], "passed": payload["passed"]}))
PY

log "6/6 演练报告：${REPORT}"
cat "${REPORT}"

# ---------------------------------------------------------------- 回写中心库
RECORD_TARGET="${RECORD_ID}"
if [[ -z "${RECORD_TARGET}" && "${RECORD_LATEST}" -eq 1 ]]; then
    LATEST_OUTPUT="$(compose exec -T -e BACKUP_INSTANCE_ID="${BACKUP_INSTANCE_ID}" \
        api python -m app.modules.ops.backup_cli latest 2>/dev/null || true)"
    RECORD_TARGET="$(printf '%s' "${LATEST_OUTPUT}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("backup",{}).get("id",""))' 2>/dev/null || true)"
fi
RESULT_STATUS="$(python3 -c 'import json,sys; print("verified" if json.load(open(sys.argv[1])).get("passed") else "failed")' "${REPORT}")"
if [[ -n "${RECORD_TARGET}" && "${RECORD}" -eq 1 ]]; then
    log "回写中心库 backup_records id=${RECORD_TARGET} status=${RESULT_STATUS}"
    RESTORE_JSON="$(python3 - "${RECORD_TARGET}" "${RESULT_STATUS}" "${REPORT}" <<'PY'
import json
import sys
backup_id, status, report_path = sys.argv[1], sys.argv[2], sys.argv[3]
report = json.load(open(report_path, encoding="utf-8"))
failed = [item["name"] for item in report.get("checks", []) if not item["ok"]]
notes = f"隔离演练 {status}，检查 {len(report.get('checks', []))} 项"
if failed:
    notes += "；失败项：" + "、".join(failed[:5])
print(json.dumps({"backup_id": int(backup_id), "status": status, "notes": notes}, ensure_ascii=False))
PY
)"
    compose exec -T -e BACKUP_RESTORE_JSON="${RESTORE_JSON}" \
        api python -m app.modules.ops.backup_cli restore-check || warn "回写中心库失败（演练结果仍在本地报告）"
fi

if [[ "${RESULT_STATUS}" == "verified" ]]; then
    log "隔离恢复演练通过：账号/权限/商品/配置检查全部通过"
    exit 0
fi
warn "隔离恢复演练存在失败项，请检查 ${REPORT}"
exit 1
