#!/usr/bin/env bash
# =============================================================================
# 服务器 git 更新部署脚本（在 /opt/ventasync 仓库根目录执行）
#
# 用法：
#   bash deploy/scripts/git_update.sh
#   bash deploy/scripts/git_update.sh --skip-migrate
#
# 行为：
#   1. git pull --ff-only（只允许快进，避免覆盖服务器改动）
#   2. 使用基础编排 + 低配服务器覆盖文件重新构建镜像
#   3. 滚动启动 api/worker/scheduler/nginx
#   4. 执行独立迁移发布任务（中心库 + 全部租户库）
#
# 约定：
#   * deploy/.env 不入库，pull 不会覆盖服务器配置；
#   * frontend/dist 已入库，服务器无需 Node；
#   * 脚本不处理备份，正式更新前请确认 /opt/backups 有当日备份。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DEPLOY_DIR}/.." && pwd)"

SKIP_MIGRATE=0
for arg in "$@"; do
    case "$arg" in
        --skip-migrate) SKIP_MIGRATE=1 ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            printf '[git_update] 未知参数：%s\n' "$arg" >&2
            exit 2
            ;;
    esac
done

log() { printf '[git_update] %s\n' "$*"; }

cd "${REPO_ROOT}"

[[ -d .git ]] || { log "错误：${REPO_ROOT} 不是 git 工作树"; exit 2; }
[[ -f "${DEPLOY_DIR}/.env" ]] || { log "错误：缺少 ${DEPLOY_DIR}/.env（服务器配置不入库）"; exit 2; }

log "拉取最新代码（只允许 fast-forward）..."
git pull --ff-only

if git ls-files -m --error-unmatch >/dev/null 2>&1; then
    log "警告：工作树存在未提交修改，请先检查（git status）"
fi

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.server.yml --env-file .env)
cd "${DEPLOY_DIR}"

log "构建镜像..."
"${COMPOSE[@]}" build

if [[ "${SKIP_MIGRATE}" -eq 0 ]]; then
    log "执行迁移发布任务（先升级中心库 / 租户库 schema）..."
    "${COMPOSE[@]}" --profile release run --rm migrate
fi

log "滚动启动服务..."
"${COMPOSE[@]}" up -d

log "当前状态："
"${COMPOSE[@]}" ps

log "健康检查："
curl -fsS "http://127.0.0.1:${HTTP_PORT:-8081}/healthz" || true
echo
log "完成"
