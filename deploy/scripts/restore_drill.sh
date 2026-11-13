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
# 当前流程：
#   1. 解密并检查清单、dump schema、最终 BASE TABLE 表名；保留原 schema。
#   2. 仅用主 MySQL 已有镜像生成独立、冻结的 Compose 配置；随机密码、无发布端口。
#   3. 禁止复用既有 drill 卷；运行时核对容器/卷标签及实际 Source 与主库不同。
#   4. PID 1 mysqld + TCP SELECT 1 连续成功后，docker cp SQL，再容器内文件重定向导入。
#   5. 表数、表名、账号、权限、商品关联、归档密钥解密及租户备份范围均通过才 passed。
#   6. 清理前重复运行时隔离断言；仅清理本次独立 MySQL，报告保留在 drill-reports。
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
CONFIG_JSON=""
REPORT=""

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
  --record-latest     回写与本次包 run_id/校验值匹配的唯一记录
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
    trap "rm -rf -- $(printf '%q' "$tmp")" EXIT
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

# ---------------------------------------------------------------- 隔离演练：只创建 MySQL，不继承生产 Compose 的卷/网络/服务
umask 077
for cmd in docker openssl gzip python3 sha256sum; do
    command -v "$cmd" >/dev/null || die "未找到 $cmd"
done
docker compose version >/dev/null
for helper in check_drill_isolation.py check_drill_runtime.py check_drill_dump.py validate_drill_data.py; do
    [[ -f "$SCRIPT_DIR/$helper" ]] || die "缺少 $helper"
done
[[ "$CENTER_DB_NAME" =~ ^[A-Za-z0-9_]+$ ]] || die "中心库名称不合法"
[[ "$MAIN_PROJECT" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "主项目名称不合法"
if [[ "$USE_LATEST" -eq 1 && -z "$BUNDLE" ]]; then
    BUNDLE="$(python3 - "$BACKUP_ROOT" <<'PY'
import pathlib,sys
files=sorted(pathlib.Path(sys.argv[1]).glob("releases/*/bundle.tar.gz.enc"))
print(files[-1] if files else "")
PY
)"
fi
[[ -f "$BUNDLE" ]] || die "请指定存在的 --bundle，或使用 --latest"
[[ -r "$BACKUP_ENCRYPTION_KEY_FILE" ]] || die "备份加密密钥不可读"
BUNDLE="$(realpath "$BUNDLE")"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PROJECT="ventasync-drill-$(date -u +%Y%m%d%H%M%S)-$RANDOM"
[[ "$PROJECT" != "$MAIN_PROJECT" ]] || die "项目名冲突"
WORK_DIR="$(mktemp -d "$BACKUP_ROOT/drill-$STAMP-XXXX")"
CONFIG_JSON="$WORK_DIR/compose.json"
REPORT_DIR="$BACKUP_ROOT/drill-reports/$PROJECT"
REPORT="$REPORT_DIR/drill-report.json"
MANIFEST="$WORK_DIR/bundle/manifest.json"
CONTAINER_ID=""
MAIN_CONTAINER_ID=""
START_ATTEMPTED=0
VALIDATION_RC=1
RECORD_WRITTEN=false
CLEANUP_STATUS=pending
mkdir -p "$WORK_DIR/bundle" "$WORK_DIR/inventory" "$WORK_DIR/sql" "$REPORT_DIR"
chmod 700 "$WORK_DIR" "$REPORT_DIR"
: > "$WORK_DIR/imports.jsonl"
export MAIN_PROJECT DRILL_PROJECT="$PROJECT"

compose() {
    # 唯一配置为本次生成且已检查的文件；不重新读取生产 YAML/.env。
    docker compose --project-directory "$WORK_DIR" -p "$PROJECT" -f "$CONFIG_JSON" "$@"
}
mysql_container_id() {
    docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" --filter label=com.docker.compose.service=mysql
}
mysql_query_at() {
    local cid="$1" sql="$2"
    docker exec "$cid" sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --protocol=TCP -h127.0.0.1 -P3306 --connect-timeout=5 --skip-reconnect -uroot --batch --raw --skip-column-names -e "$1"' sh "$sql"
}
mysql_query() { mysql_query_at "$CONTAINER_ID" "$1"; }
quote_identifier() { printf '\140%s\140' "$1"; }
validate_runtime() {
    [[ -n "$CONTAINER_ID" && "$CONTAINER_ID" != *$'\n'* ]] || return 1
    docker inspect "$CONTAINER_ID" > "$WORK_DIR/drill-container.json" || return 1
    docker inspect "$MAIN_CONTAINER_ID" > "$WORK_DIR/main-container.json" || return 1
    docker volume inspect "${PROJECT}_mysql_data" > "$WORK_DIR/drill-volume.json" || return 1
    python3 "$SCRIPT_DIR/check_drill_runtime.py" --config "$CONFIG_JSON" \
        --project "$PROJECT" --main-project "$MAIN_PROJECT" \
        --container "$WORK_DIR/drill-container.json" --volume "$WORK_DIR/drill-volume.json" \
        --main-container "$WORK_DIR/main-container.json" > "$WORK_DIR/isolation.json"
}
write_report() {
    local exit_code="$1"
    python3 - "$WORK_DIR" "$REPORT" "$BUNDLE" "$PROJECT" "$MAIN_PROJECT" "$exit_code" "$RECORD_WRITTEN" "$CLEANUP_STATUS" <<'PY'
import hashlib,json,sys
from pathlib import Path
from datetime import datetime,timezone
work,report,bundle,project,main,rc,recorded,cleanup_status=sys.argv[1:]
work=Path(work)
def load(name,default):
    path=work/name
    try:
        return json.loads(path.read_text()) if path.is_file() else default
    except (ValueError,OSError):
        return default
manifest=load("bundle/manifest.json",{})
validation=load("validation.json",{"checks":[],"passed":False})
imports=[json.loads(line) for line in (work/"imports.jsonl").read_text().splitlines() if line]
checks=validation.get("checks",[])
checks += [{"name":"import:"+i["database"],"ok":i["before"]==0 and i["after"]==i["expected"] and i["table_names_match"],"detail":i} for i in imports]
isolation=load("isolation.json",{})
checks.append({"name":"isolated_mysql_volume","ok":isolation.get("ok") is True})
checks.append({"name":"all_archive_databases_imported","ok":bool(imports) and sorted(i["database"] for i in imports)==sorted(manifest.get("databases",[]))})
checks.append({"name":"script_completed","ok":int(rc)==0})
with open(bundle,"rb") as f: digest=hashlib.file_digest(f,"sha256").hexdigest()
payload={"drill_at":datetime.now(timezone.utc).isoformat(),"run_id":manifest.get("run_id"),"bundle":bundle,
 "bundle_sha256":digest,
 "target":{**isolation,"project":project,"main_project":main,"readiness":"PID 1 mysqld + TCP SELECT 1 连续三次成功","schema_policy":"保留 dump 原 schema，仅写 drill 独立卷"},
 "imports":imports,"checks":checks,"failed":sum(c.get("ok") is not True for c in checks),
 "passed":bool(validation.get("passed")) and bool(checks) and all(c.get("ok") is True for c in checks),
 "record_written":recorded=="true",
 "cleanup_status":cleanup_status,
 "scope":"数据库恢复、账号/权限/商品关联及配置解密；未启动业务服务，未做交互登录或云服务连通测试"}
Path(report).write_text(json.dumps(payload,ensure_ascii=False,indent=2))
print(json.dumps({"report":report,"passed":payload["passed"],"failed":payload["failed"]},ensure_ascii=False))
PY
}
cleanup() {
    local result=$? cleanup_ok=0
    trap - EXIT
    set +e
    if [[ "$KEEP" -eq 1 && "$DRY_RUN" -ne 1 ]]; then
        CLEANUP_STATUS=kept
        log "保留 drill：project=$PROJECT work=$WORK_DIR"
    elif [[ "$START_ATTEMPTED" -eq 1 ]]; then
        if validate_runtime; then
            compose down --volumes --timeout 10 && cleanup_ok=1
        fi
        if [[ "$cleanup_ok" -eq 1 ]]; then
            CLEANUP_STATUS=removed
        else
            CLEANUP_STATUS=failed_or_refused
            warn "自动清理未完成；保留本次 drill 现场 $WORK_DIR，不扩大删除范围"
            result=1
        fi
    else
        CLEANUP_STATUS=not_started
    fi
    if [[ "$DRY_RUN" -ne 1 ]]; then
        write_report "$result" || { warn "报告写入失败"; result=1; }
    fi
    if [[ "$CLEANUP_STATUS" == removed || "$CLEANUP_STATUS" == not_started ]]; then
        rm -rf -- "$WORK_DIR"
    fi
    exit "$result"
}
trap cleanup EXIT

log "1/7 校验、解密备份包 $BUNDLE"
if [[ -f "$BUNDLE.sha256" ]]; then
    (cd "$(dirname "$BUNDLE")" && sha256sum -c "$(basename "$BUNDLE").sha256" >/dev/null) || die "加密包校验失败"
fi
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in "$BUNDLE" \
    -out "$WORK_DIR/bundle.tar.gz" -pass "file:$BACKUP_ENCRYPTION_KEY_FILE"
python3 - "$WORK_DIR/bundle.tar.gz" "$WORK_DIR/bundle" <<'PY'
import hashlib,json,sys,tarfile
from pathlib import Path,PurePosixPath
root=Path(sys.argv[2]).resolve()
with tarfile.open(sys.argv[1],"r:gz") as tar:
    for item in tar.getmembers():
        path=PurePosixPath(item.name)
        if path.is_absolute() or ".." in path.parts or not (item.isfile() or item.isdir()):
            raise SystemExit("备份包含不安全路径/链接")
    tar.extractall(root)
manifest=json.loads((root/"manifest.json").read_text())
for item in manifest.get("files",[]):
    path=(root/item["path"]).resolve()
    if not path.is_relative_to(root): raise SystemExit("清单路径越界")
    if not path.is_file():
        if path.name==".skipped_dbs": continue
        raise SystemExit("清单文件缺失: "+item["path"])
    if path.name.endswith(".enc"): continue
    with open(path,"rb") as f: digest=hashlib.file_digest(f,"sha256").hexdigest()
    if item.get("sha256") and digest!=item["sha256"]:
        raise SystemExit("清单校验失败: "+item["path"])
actual=sorted(p.name[:-7] for p in root.glob("*.sql.gz"))
if not actual or actual!=sorted(manifest.get("databases",[])):
    raise SystemExit("清单 databases 与归档 SQL 文件不一致")
print(json.dumps({"run_id":manifest.get("run_id"),"databases":actual}))
PY

log "2/7 检查 dump 的 schema 声明和表清单"
CENTER_SQL="$WORK_DIR/bundle/$CENTER_DB_NAME.sql.gz"
[[ -f "$CENTER_SQL" ]] || die "缺少中心库 dump"
SQLS=("$CENTER_SQL")
for sql in "$WORK_DIR"/bundle/*.sql.gz; do
    [[ "$sql" == "$CENTER_SQL" ]] || SQLS+=("$sql")
done
for sql in "${SQLS[@]}"; do
    db="$(basename "$sql" .sql.gz)"
    python3 "$SCRIPT_DIR/check_drill_dump.py" --file "$sql" --database "$db" \
        --output "$WORK_DIR/inventory/$db.json" --sql-output "$WORK_DIR/sql/$db.sql"
done

log "3/7 生成并冻结独立 MySQL 配置"
MAIN_CONTAINER_ID="$(docker ps -q --filter "label=com.docker.compose.project=$MAIN_PROJECT" --filter label=com.docker.compose.service=mysql)"
[[ -n "$MAIN_CONTAINER_ID" && "$MAIN_CONTAINER_ID" != *$'\n'* ]] || die "无法唯一定位主 MySQL，拒绝演练"
docker inspect "$MAIN_CONTAINER_ID" > "$WORK_DIR/main-container.json"
# 只借用本机已有 MySQL 镜像；密码随机生成，不复用生产密码。
python3 - "$WORK_DIR/main-container.json" "$CONFIG_JSON" "$PROJECT" <<'PY'
import json,secrets,sys
from pathlib import Path
main=json.loads(Path(sys.argv[1]).read_text())[0]
project=sys.argv[3]
config={"name":project,"services":{"mysql":{
 "image":main["Image"],"pull_policy":"never","restart":"no","mem_limit":"768m",
 "environment":{"MYSQL_ROOT_PASSWORD":secrets.token_hex(24)},
 "command":["--character-set-server=utf8mb4","--collation-server=utf8mb4_unicode_ci","--innodb-buffer-pool-size=128M","--max-connections=30","--performance-schema=OFF"],
 "healthcheck":{"disable":True},"ports":[],
 "volumes":[{"type":"volume","source":"mysql_data","target":"/var/lib/mysql"}],
 "networks":{"default":None}}},
 "volumes":{"mysql_data":{"name":project+"_mysql_data"}},
 "networks":{"default":{"name":project+"_default","internal":True}}}
Path(sys.argv[2]).write_text(json.dumps(config))
PY
chmod 600 "$CONFIG_JSON"
python3 "$SAFETY_CHECKER" --project "$PROJECT" --main-project "$MAIN_PROJECT" --config "$CONFIG_JSON"
compose config --quiet
if [[ "$DRY_RUN" -eq 1 ]]; then
    log "dry-run 通过：归档/schema/独立卷配置有效，未启动容器、未导入"
    exit 0
fi
if [[ "$ASSUME_YES" -ne 1 ]]; then
    printf '确认独立演练请输入项目名 %s：' "$PROJECT"
    read -r answer
    [[ "$answer" == "$PROJECT" ]] || die "已取消"
fi
[[ -z "$(mysql_container_id)" ]] || die "drill 项目已存在，拒绝复用"
if docker volume inspect "${PROJECT}_mysql_data" >/dev/null 2>&1; then
    die "drill 卷已存在，拒绝复用"
fi
if docker network inspect "${PROJECT}_default" >/dev/null 2>&1; then
    die "drill 网络已存在，拒绝复用"
fi

wait_for_mysql_ready() {
    local deadline=$((SECONDS+180)) consecutive=0 value pid1
    log "等待 PID 1 正式 mysqld + TCP 3306 SELECT 1（最多 180s，连续三次）"
    while (( SECONDS < deadline )); do
        [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_ID")" == true ]] || return 1
        pid1="$(docker exec "$CONTAINER_ID" cat /proc/1/comm 2>/dev/null)" || pid1=""
        value=""
        if [[ "$pid1" == mysqld ]] && value="$(mysql_query 'SELECT 1' 2>/dev/null)" && [[ "$value" == 1 ]]; then
            consecutive=$((consecutive+1))
        else
            consecutive=0
        fi
        [[ "$consecutive" -lt 3 ]] || return 0
        sleep 2
    done
    return 1
}
db_base_table_count() {
    local value
    value="$(mysql_query "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$1' AND table_type='BASE TABLE'")" || return 1
    [[ "$value" =~ ^[0-9]+$ ]] || { warn "表数查询返回非法结果"; return 1; }
    printf '%s\n' "$value"
}
validate_drill_datadir() {
    validate_runtime || die "运行时隔离校验未通过，禁止导入"
    local datadir
    datadir="$(mysql_query 'SELECT @@datadir')" || die "datadir 查询失败"
    [[ "$datadir" == /var/lib/mysql/ || "$datadir" == /var/lib/mysql ]] || die "异常 datadir"
    [[ "$(mysql_query 'SELECT @@read_only')" == 0 ]] || die "隔离 MySQL 不可写"
    log "运行时隔离通过：$CONTAINER_ID / ${PROJECT}_mysql_data，与主 MySQL 数据目录不同"
}
assert_target_databases_fresh() {
    local sql db count
    for sql in "${SQLS[@]}"; do
        db="$(basename "$sql" .sql.gz)"
        count="$(db_base_table_count "$db")" || die "$db 表数查询失败"
        [[ "$count" == 0 ]] || die "$db 已有 $count 表，拒绝覆盖"
    done
}
mysql_import() {
    local db="$1" local_sql="$WORK_DIR/sql/$1.sql" remote_sql="/tmp/restore-$1.sql" expected_hash actual_hash
    expected_hash="$(sha256sum "$local_sql")"; expected_hash="${expected_hash%% *}"
    docker cp "$local_sql" "$CONTAINER_ID:$remote_sql" || return 1
    actual_hash="$(docker exec "$CONTAINER_ID" sha256sum "$remote_sql")" || return 1
    [[ "${actual_hash%% *}" == "$expected_hash" ]] || { warn "容器内 SQL 校验值不一致"; return 1; }
    mysql_query "CREATE DATABASE IF NOT EXISTS $(quote_identifier "$db") CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" || return 1
    # 文件由容器内 shell 打开，SQL 不经过 SSH/stdin/docker exec -i 管道。
    docker exec "$CONTAINER_ID" sh -c '
        MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=TCP -h127.0.0.1 -P3306 \
          --connect-timeout=5 --skip-reconnect --binary-mode -uroot --batch --database="$1" < "$2"
    ' sh "$db" "$remote_sql" || return 1
    docker exec "$CONTAINER_ID" rm -f -- "$remote_sql"
}
import_and_verify() {
    local db="$1" before actual
    before="$(db_base_table_count "$db")" || die "$db 导入前查询失败"
    [[ "$before" == 0 ]] || die "$db 导入前非空"
    log "导入 schema=$db，导入前表数=$before"
    mysql_import "$db" || die "$db 文件导入失败（以 MySQL 实际错误为准）"
    actual="$(db_base_table_count "$db")" || die "$db 导入后查询失败"
    mysql_query "SELECT table_name FROM information_schema.tables WHERE table_schema='$db' AND table_type='BASE TABLE' ORDER BY table_name" > "$WORK_DIR/$db.actual-tables" || die "$db 表名查询失败"
    python3 - "$WORK_DIR" "$db" "$before" "$actual" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); db=sys.argv[2]
inventory=json.loads((root/"inventory"/(db+".json")).read_text())
actual_names=(root/(db+".actual-tables")).read_text().splitlines()
item={"database":db,"before":int(sys.argv[3]),"after":int(sys.argv[4]),"expected":inventory["table_count"],
 "table_names_match":sorted(actual_names)==sorted(inventory["table_names"]),
 "schema_declarations":inventory["schema_declarations"],"sql_sha256":inventory["sql_sha256"]}
with (root/"imports.jsonl").open("a") as out: out.write(json.dumps(item)+"\n")
print(json.dumps(item,ensure_ascii=False))
if item["before"]!=0 or item["expected"]<=0 or item["after"]!=item["expected"] or not item["table_names_match"]:
    raise SystemExit("导入硬校验失败：表数/表名必须与 dump 完全一致")
PY
}
log "4/7 启动隔离 MySQL（仅本次新卷，无端口发布）"
START_ATTEMPTED=1
compose up -d --no-deps mysql
CONTAINER_ID="$(mysql_container_id)"
validate_runtime || die "启动后隔离断言失败"
wait_for_mysql_ready || { docker logs --tail 40 "$CONTAINER_ID" >&2; die "正式 MySQL TCP 认证未就绪"; }
validate_drill_datadir
assert_target_databases_fresh
log "5/7 文件导入与逐库硬校验"
for sql in "${SQLS[@]}"; do
    import_and_verify "$(basename "$sql" .sql.gz)"
done
log "6/7 校验账号/权限/商品关联、配置解密和备份范围"
if python3 "$SCRIPT_DIR/validate_drill_data.py" --container "$CONTAINER_ID" --center "$CENTER_DB_NAME" \
    --manifest "$MANIFEST" --env-file "$WORK_DIR/bundle/config/.env" --output "$WORK_DIR/validation.json"; then
    VALIDATION_RC=0
else
    VALIDATION_RC=1
fi
write_report "$VALIDATION_RC"
[[ "$VALIDATION_RC" -eq 0 ]] || die "数据验证存在失败项，详见 $REPORT（不得标记 verified）"

# 显式请求回写时，ID、run_id、加密包校验值必须同时匹配。
if [[ -n "$RECORD_ID" || "$RECORD_LATEST" -eq 1 ]]; then
    [[ "$BACKUP_RECORD" == 1 ]] || die "BACKUP_RECORD 禁止回写"
    run_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$MANIFEST")"
    [[ "$run_id" =~ ^[A-Za-z0-9_-]+$ ]] || die "run_id 不合法"
    digest="$(sha256sum "$BUNDLE")"; digest="${digest%% *}"
    record_filter="JSON_UNQUOTE(JSON_EXTRACT(manifest_json,'$.run_id'))='$run_id' AND checksum='sha256:$digest'"
    if [[ -n "$RECORD_ID" ]]; then
        [[ "$RECORD_ID" =~ ^[1-9][0-9]*$ ]] || die "record-id 不合法"
        record_filter="$record_filter AND id=$RECORD_ID"
    fi
    record_target="$(mysql_query_at "$MAIN_CONTAINER_ID" "SELECT id FROM $(quote_identifier "$CENTER_DB_NAME").backup_records WHERE $record_filter")"
    [[ "$record_target" =~ ^[1-9][0-9]*$ ]] || die "未找到唯一匹配本次 run_id/校验值的备份记录；未回写"
    api_id="$(docker ps -q --filter "label=com.docker.compose.project=$MAIN_PROJECT" --filter label=com.docker.compose.service=api)"
    [[ -n "$api_id" && "$api_id" != *$'\n'* ]] || die "无法唯一定位用于回写的主 API"
    restore_json="$(python3 -c 'import json,sys; print(json.dumps({"backup_id":int(sys.argv[1]),"status":"verified","notes":"隔离恢复硬校验及数据关联/配置解密全部通过；报告 "+sys.argv[2]}))' "$record_target" "$REPORT")"
    docker exec -e BACKUP_RESTORE_JSON="$restore_json" "$api_id" python -m app.modules.ops.backup_cli restore-check > "$WORK_DIR/record-result.json" || die "回写失败，恢复报告保留"
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("ok") is True and d.get("backup",{}).get("restore_status")=="verified" else 1)' "$WORK_DIR/record-result.json" || die "回写未返回 verified"
    RECORD_WRITTEN=true
fi
log "7/7 隔离恢复验证通过；报告将保留在 $REPORT"
