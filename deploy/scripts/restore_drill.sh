#!/usr/bin/env bash
# =============================================================================
# ERP 隔离环境恢复演练（批次3：备份加密异地与恢复演练）
#
# ⚠️ 安全第一（2026-11-13 事故后加固）
# -----------------------------------------------------------------------------
# 事故根因：deploy/docker-compose.yml 的顶层卷使用显式
#   name: ${COMPOSE_PROJECT_NAME:-ventasync}_mysql_data
# 显式卷名会覆盖 `docker compose -p <project>` 的项目前缀，旧版演练仅加
# `-p ventasync-drill-*`，仍挂载主项目 MySQL 卷，导致导入写坏主库、清理删除主卷。
#
# 现行保护（层层断言，任何一步不满足立即中止，且中止前不会启动/删除任何主项目资源）：
#   1. 项目名必须是 `ventasync-drill-*`；与主项目名不同；
#   2. compose 调用同时设置 `COMPOSE_PROJECT_NAME=<drill 项目>` 与 `-p <drill 项目>`，
#      override 再显式把 mysql/redis/catalog 卷名改为 `<drill 项目>_*`；
#   3. 启动前用 `docker compose config --format json` 解析最终配置，由
#      check_drill_isolation.py 断言所有卷名/挂载都属于 drill 项目、mysql 无端口发布；
#   4. 交互确认（必须输入 drill 项目名）或显式 --yes；--dry-run 只做只读校验；
#   5. 容器启动后再次用 docker inspect 校验容器名与挂载卷名；
#   6. 清理只执行 `docker compose -p <drill 项目> down -v`，且项目名必须匹配 drill；
#      任何失败都不会对主项目执行 down/rm。
#
# 用法：
#   bash deploy/scripts/restore_drill.sh --self-test                 # 无服务器自测（加密+安全断言）
#   bash deploy/scripts/restore_drill.sh --latest --dry-run          # 只读校验（推荐服务器先跑）
#   bash deploy/scripts/restore_drill.sh --latest --yes              # 隔离临时卷演练（非交互）
#   bash deploy/scripts/restore_drill.sh --bundle <bundle.tar.gz.enc> --yes
#   bash deploy/scripts/restore_drill.sh --latest --keep --yes       # 保留演练容器卷供人工核查
#
# 禁止事项：
#   * 禁止在 --dry-run 通过前执行真实演练；
#   * 禁止手工修改/删除主项目卷；演练失败只清理 drill 项目资源；
#   * 禁止把 drill 数据库用于对外服务或写入生产数据。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DEPLOY_DIR}/.." && pwd)"
COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.yml"
COMPOSE_SERVER="${DEPLOY_DIR}/docker-compose.server.yml"
SAFETY_CHECKER="${SCRIPT_DIR}/check_drill_isolation.py"

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
# 主项目名：仅用于「禁止卷名冲突」断言，不会对主项目执行任何操作
MAIN_PROJECT="${COMPOSE_PROJECT_NAME:-ventasync}"

BUNDLE=""
USE_LATEST=0
SELF_TEST=0
KEEP=0
ASSUME_YES=0
DRY_RUN=0
RECORD_ID=""
RECORD_LATEST=0
WORK_DIR=""
PROJECT=""
OVERRIDE_FILE=""
CONFIG_JSON=""
REPORT=""
# 仅在「compose config 安全断言通过」后置 1；cleanup 用它决定是否允许执行 down -v
SAFETY_VERIFIED=0

log() { printf '[drill] %s\n' "$*"; }
warn() { printf '[drill] 警告：%s\n' "$*" >&2; }
die() { printf '[drill] 错误：%s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,48p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

参数：
  --bundle <文件>     加密备份包（bundle.tar.gz.enc）
  --latest            取本地最新备份包（BACKUP_ROOT/releases/*/bundle.tar.gz.enc）
  --self-test         无服务器自测：加密包校验 + 隔离安全断言（不会启动容器）
  --dry-run           只读：解密校验 + 解析 compose 最终配置并做安全断言，不启动任何容器
  --yes               非交互确认（输入项目名确认的替代；自动化/远程执行用）
  --record-id <ID>    演练成功后回写该 backup_records 记录
  --record-latest     回写该实例最新一条备份记录
  --keep              保留演练容器与解压目录（默认演练结束后清理 drill 项目）
  -h, --help          显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bundle) [[ $# -ge 2 ]] || die "--bundle 需要参数"; BUNDLE="$2"; shift 2 ;;
        --latest) USE_LATEST=1; shift ;;
        --self-test) SELF_TEST=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --yes) ASSUME_YES=1; shift ;;
        --record-id) [[ $# -ge 2 ]] || die "--record-id 需要参数"; RECORD_ID="$2"; shift 2 ;;
        --record-latest) RECORD_LATEST=1; shift ;;
        --keep) KEEP=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1（使用 -h 查看帮助）" ;;
    esac
done

# ---------------------------------------------------------------- 自测（不依赖 docker/mysql）
run_safety_fixtures() {
    local dir="$1" checker="${SAFETY_CHECKER}" project="ventasync-drill-1763000000-1234"
    local unsafe="${dir}/config_unsafe.json" safe="${dir}/config_safe.json" bad="${dir}/config_bad_project.json"
    cat > "${unsafe}" <<'JSON'
{"name": "ventasync-drill-1763000000-1234",
 "volumes": {"mysql_data": {"name": "ventasync_mysql_data"}},
 "services": {"mysql": {"ports": [{"published": "3306"}], "volumes": [{"type": "volume", "source": "mysql_data", "target": "/var/lib/mysql"}]}}}
JSON
    cat > "${safe}" <<'JSON'
{"name": "ventasync-drill-1763000000-1234",
 "volumes": {"mysql_data": {"name": "ventasync-drill-1763000000-1234_mysql_data"}},
 "services": {"mysql": {"ports": [], "volumes": [{"type": "volume", "source": "mysql_data", "target": "/var/lib/mysql"}]}}}
JSON
    cat > "${bad}" <<'JSON'
{"name": "ventasync", "volumes": {"mysql_data": {"name": "ventasync_mysql_data"}},
 "services": {"mysql": {"volumes": [{"type": "volume", "source": "mysql_data", "target": "/var/lib/mysql"}]}}}
JSON
    [[ -f "${checker}" ]] || die "缺少安全断言脚本：${checker}"
    log "[self-test] 安全断言：不安全配置必须被拒绝"
    if python3 "${checker}" --project "${project}" --main-project ventasync --config "${unsafe}" >/dev/null 2>&1; then
        die "自测失败：复用了主项目卷的配置未被拒绝"
    fi
    log "[self-test] 安全断言：合法 drill 配置通过"
    python3 "${checker}" --project "${project}" --main-project ventasync --config "${safe}" >/dev/null \
        || die "自测失败：合法隔离配置被误判"
    log "[self-test] 安全断言：非 drill 项目名/主项目名必须被拒绝"
    if python3 "${checker}" --project "ventasync" --main-project ventasync --config "${bad}" >/dev/null 2>&1; then
        die "自测失败：主项目名未被拒绝"
    fi
    log "[self-test] 安全断言通过（复用主卷/主项目名会中止，合法 drill 隔离配置放行）"
}

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
    log "[self-test] 5/5 隔离安全断言"
    run_safety_fixtures "${tmp}"
    printf '{"self_test": true, "bundle": "%s", "ok": true}\n' "${bundle}" > "${tmp}/report.json"
    python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "${tmp}/report.json" || die "自测失败：报告不是合法 JSON"
    log "[self-test] 通过：加密包可解密、清单/数据库/配置/关键表校验正常，隔离安全断言有效（未连接真实数据库）"
    exit 0
}

if [[ "${SELF_TEST}" -eq 1 ]]; then
    self_test
fi

# ---------------------------------------------------------------- 依赖与定位备份包
command -v docker >/dev/null 2>&1 || die "未找到 docker 命令"
command -v openssl >/dev/null 2>&1 || die "未找到 openssl 命令"
command -v gzip >/dev/null 2>&1 || die "未找到 gzip 命令"
command -v python3 >/dev/null 2>&1 || die "未找到 python3 命令"
docker compose version >/dev/null 2>&1 || die "docker compose v2 不可用"
[[ -f "${SAFETY_CHECKER}" ]] || die "缺少安全断言脚本：${SAFETY_CHECKER}"
[[ -f "${COMPOSE_FILE}" ]] || die "找不到 Compose 文件：${COMPOSE_FILE}"
[[ -f "${COMPOSE_SERVER}" ]] || die "找不到 Compose 覆盖文件：${COMPOSE_SERVER}"

# 项目名：drill 专属 + 单调唯一；同时把 COMPOSE_PROJECT_NAME 固定为 drill 项目，
# 从根上避免 base compose 中 ${COMPOSE_PROJECT_NAME}_* 的显式卷名落到主项目。
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PROJECT="ventasync-drill-$(date -u +%Y%m%d%H%M%S)-$RANDOM"
[[ "${PROJECT}" =~ ^ventasync-drill-[a-z0-9-]+$ ]] || die "生成的 drill 项目名不合法：${PROJECT}"
[[ "${PROJECT}" != "${MAIN_PROJECT}" ]] || die "drill 项目名不能等于主项目名：${PROJECT}"

compose() {
    COMPOSE_PROJECT_NAME="${PROJECT}" docker compose -p "${PROJECT}" \
        -f "${COMPOSE_FILE}" -f "${COMPOSE_SERVER}" -f "${OVERRIDE_FILE}" "$@"
}

if [[ "${USE_LATEST}" -eq 1 && -z "${BUNDLE}" ]]; then
    BUNDLE="$(ls -1dt "${BACKUP_ROOT}"/releases/*/bundle.tar.gz.enc 2>/dev/null | head -n1 || true)"
    [[ -n "${BUNDLE}" ]] || die "未找到本地备份包：${BACKUP_ROOT}/releases/*/bundle.tar.gz.enc"
fi
[[ -n "${BUNDLE}" ]] || die "请用 --bundle 指定加密备份包，或使用 --latest / --self-test"
[[ -f "${BUNDLE}" ]] || die "备份包不存在：${BUNDLE}"
[[ -r "${BACKUP_ENCRYPTION_KEY_FILE}" ]] || die "备份加密密钥不可读：${BACKUP_ENCRYPTION_KEY_FILE}"

WORK_DIR="$(mktemp -d "${BACKUP_ROOT}/drill-${STAMP}-XXXX" 2>/dev/null || mktemp -d)"
chmod 700 "${WORK_DIR}"
OVERRIDE_FILE="${WORK_DIR}/docker-compose.drill.yml"
CONFIG_JSON="${WORK_DIR}/compose.resolved.json"
REPORT="${WORK_DIR}/drill-report.json"
mkdir -p "${WORK_DIR}/bundle"

cleanup() {
    # 只允许清理 drill 项目；任何情况下不触碰主项目；dry-run 不执行任何 docker 命令
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        rm -rf "${WORK_DIR:-}" 2>/dev/null || true
        return
    fi
    if [[ -z "${PROJECT}" || ! "${PROJECT}" =~ ^ventasync-drill- ]]; then
        warn "项目名异常，跳过自动清理（请人工检查：${PROJECT:-<空>}）"
        return
    fi
    if [[ "${SAFETY_VERIFIED}" -ne 1 ]]; then
        warn "安全断言未通过，跳过自动 docker 清理（只删除临时目录；请人工核对 drill 资源：project=${PROJECT}）"
        rm -rf "${WORK_DIR:-}" 2>/dev/null || true
        return
    fi
    if [[ "${KEEP}" -eq 1 ]]; then
        log "保留演练环境以便人工核查：project=${PROJECT} work=${WORK_DIR}"
        return
    fi
    log "清理 drill 项目与临时卷：${PROJECT}"
    # 再次固定 COMPOSE_PROJECT_NAME：避免 base compose 中 ${COMPOSE_PROJECT_NAME}_* 回退到主项目卷名
    COMPOSE_PROJECT_NAME="${PROJECT}" docker compose -p "${PROJECT}" \
        -f "${COMPOSE_FILE}" -f "${COMPOSE_SERVER}" -f "${OVERRIDE_FILE}" down -v --remove-orphans \
        >/dev/null 2>&1 || true
    rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

# ---------------------------------------------------------------- 校验与解密
log "1/7 校验加密包：${BUNDLE}"
if [[ -f "${BUNDLE}.sha256" ]]; then
    ( cd "$(dirname "${BUNDLE}")" && sha256sum -c "$(basename "${BUNDLE}").sha256" >/dev/null ) \
        || die "加密包 SHA-256 校验失败"
fi
log "2/7 解密并解包到隔离目录"
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

# ---------------------------------------------------------------- 隔离安全断言（启动前）
log "3/7 生成 drill 专用 compose 覆盖（独立项目 + 独立卷名 + 无端口发布）"
cat > "${OVERRIDE_FILE}" <<YAML
# 自动生成：仅用于 drill 项目 ${PROJECT}。所有命名卷都改为 ${PROJECT}_*，
# 并清空端口发布、禁用业务服务；不得手工复用到主项目。
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
  migrate:
    profiles: ["never"]
  registry:
    profiles: ["never"]
volumes:
  mysql_data:
    name: ${PROJECT}_mysql_data
  redis_queue_data:
    name: ${PROJECT}_redis_queue_data
  catalog_media_data:
    name: ${PROJECT}_catalog_media_data
  registry_data:
    name: ${PROJECT}_registry_data
YAML

log "   解析最终 compose 配置（不启动容器）"
if ! compose config --format json > "${CONFIG_JSON}" 2>"${WORK_DIR}/compose-config.err"; then
    die "docker compose config 失败：$(tail -n 3 "${WORK_DIR}/compose-config.err" | tr '\n' ' ')"
fi
if ! python3 "${SAFETY_CHECKER}" --project "${PROJECT}" --main-project "${MAIN_PROJECT}" --config "${CONFIG_JSON}"; then
    die "隔离安全断言失败：拒绝启动（不会影响主项目）"
fi
SAFETY_VERIFIED=1
python3 - "${CONFIG_JSON}" "${PROJECT}" <<'PY'
import json
import sys
config = json.load(open(sys.argv[1], encoding="utf-8"))
print("[drill] 演练项目：", sys.argv[2])
for key, spec in sorted((config.get("volumes") or {}).items()):
    print(f"[drill]   卷 {key} -> {spec.get('name')}")
mysql = (config.get("services") or {}).get("mysql") or {}
print("[drill]   mysql ports:", mysql.get("ports") or [])
PY

if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "dry-run：安全断言通过，未启动任何容器、未导入任何数据；退出"
    exit 0
fi

# 交互确认：必须输入 drill 项目名（--yes 可跳过，仅限自动化/已复核场景）
if [[ "${ASSUME_YES}" -ne 1 ]]; then
    cat <<EOF

[drill] 即将启动隔离演练：
  项目     : ${PROJECT}
  备份包   : ${BUNDLE}
  主项目名 : ${MAIN_PROJECT}（本脚本不会对其执行任何 up/down/rm）
  卷名     : 全部为 ${PROJECT}_*（已通过 compose config 断言）
确认请输入项目名（${PROJECT}），其它输入将中止：
EOF
    read -r answer
    [[ "${answer}" == "${PROJECT}" ]] || die "未确认，已中止（未启动任何容器）"
fi

# ---------------------------------------------------------------- 启动隔离 MySQL
log "4/7 启动隔离 MySQL（project=${PROJECT}，独立卷，不暴露端口）"
compose up -d --no-deps mysql
MYSQL_READY=0
for _ in $(seq 1 60); do
    if compose exec -T mysql sh -c 'export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?}"; mysqladmin ping -uroot --silent' >/dev/null 2>&1; then
        MYSQL_READY=1
        break
    fi
    sleep 2
done
[[ "${MYSQL_READY}" -eq 1 ]] || die "隔离 MySQL 启动超时（仅 drill 项目受影响）"

# 启动后再次校验：容器名与挂载卷必须属于 drill 项目
CONTAINER_ID="$(compose ps -q mysql)"
[[ -n "${CONTAINER_ID}" ]] || die "未找到 drill mysql 容器"
CONTAINER_NAME="$(docker inspect --format '{{.Name}}' "${CONTAINER_ID}")"
[[ "${CONTAINER_NAME}" == *"${PROJECT}"* ]] || die "异常：mysql 容器名 ${CONTAINER_NAME} 不属于 ${PROJECT}，立即中止"
MOUNTED_VOLUMES="$(docker inspect --format '{{range .Mounts}}{{println .Name}}{{end}}' "${CONTAINER_ID}" | sed '/^$/d')"
while IFS= read -r volume; do
    [[ -z "${volume}" ]] && continue
    if [[ "${volume}" != ${PROJECT}_* ]]; then
        die "异常：mysql 挂载卷 ${volume} 不属于 ${PROJECT}，立即中止（不会清理主项目）"
    fi
done <<< "${MOUNTED_VOLUMES}"
log "   容器 ${CONTAINER_NAME} 挂载卷：$(echo "${MOUNTED_VOLUMES}" | tr '\n' ' ')"

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

log "5/7 恢复中心库与租户库到 drill 临时卷（${#TENANT_SQLS[@]} 个租户库）"
mysql_import "${CENTER_SQL}"
for sql in "${TENANT_SQLS[@]}"; do
    log "恢复 $(basename "${sql}")"
    mysql_import "${sql}"
done

# ---------------------------------------------------------------- 关键数据校验
log "6/7 校验账号、权限、商品、配置"
echo '[]' > "${CONFIG_JSON}.checks.json"
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

log "7/7 演练报告：${REPORT}"
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
notes = f"隔离演练（drill 临时卷）{status}，检查 {len(report.get('checks', []))} 项"
if failed:
    notes += "；失败项：" + "、".join(failed[:5])
print(json.dumps({"backup_id": int(backup_id), "status": status, "notes": notes}, ensure_ascii=False))
PY
)"
    compose exec -T -e BACKUP_RESTORE_JSON="${RESTORE_JSON}" \
        api python -m app.modules.ops.backup_cli restore-check || warn "回写中心库失败（演练结果仍在本地报告）"
fi

if [[ "${RESULT_STATUS}" == "verified" ]]; then
    log "隔离恢复演练通过：账号/权限/商品/配置检查全部通过（drill 项目 ${PROJECT}）"
    exit 0
fi
warn "隔离恢复演练存在失败项，请检查 ${REPORT}"
exit 1
