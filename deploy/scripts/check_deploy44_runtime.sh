#!/usr/bin/env bash
# =============================================================================
# task-44：Redis 队列内存 / Worker 执行位只读巡检（禁止变更运行中的容器）
#
# 用途：上线窗口前确认 compose 配置、Redis maxmemory 与容器上限余量、
# AOF/noeviction 策略、普通队列与 tiktok_refresh 队列积压。
#
# 安全约束：
# * 只执行 `docker compose config/ps/exec redis-cli INFO/LLEN`，全部只读；
# * 不执行 up / restart / down / CONFIG SET，不改任何运行中容器；
# * 生产 `deploy/.env` 中的 QUEUE_REDIS_MAXMEMORY 才是实际值，本脚本用于核对它。
#
# 用法（仓库根目录）：
#   bash deploy/scripts/check_deploy44_runtime.sh                 # 低配覆盖 + .env
#   bash deploy/scripts/check_deploy44_runtime.sh --base          # 仅基础 compose
#   ENV_FILE=deploy/.env bash deploy/scripts/check_deploy44_runtime.sh
#   bash deploy/scripts/check_deploy44_runtime.sh --config-only   # 不访问容器
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

ENV_FILE="${ENV_FILE:-deploy/.env}"
COMPOSE=(docker compose -f deploy/docker-compose.yml)
CONFIG_ONLY=0
USE_BASE_ONLY=0

for arg in "$@"; do
  case "${arg}" in
    --config-only) CONFIG_ONLY=1 ;;
    --base) USE_BASE_ONLY=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "未知参数：${arg}" >&2; exit 2 ;;
  esac
done

if [[ "${USE_BASE_ONLY}" -eq 0 ]]; then
  COMPOSE+=(-f deploy/docker-compose.server.yml)
fi

if [[ -f "${ENV_FILE}" ]]; then
  COMPOSE+=(--env-file "${ENV_FILE}")
else
  echo "[WARN] 未找到 ${ENV_FILE}，改用 deploy/.env.example 仅做离线校验" >&2
  COMPOSE+=(--env-file deploy/.env.example)
  ENV_FILE="deploy/.env.example"
fi
# 巡检包含专用 Worker；默认不启用 profile 不会因此启动它，只会让 ps/config 能看到服务。
COMPOSE+=(--profile tiktok-refresh)

echo "== 1) compose config 校验（不启动容器） =="
"${COMPOSE[@]}" config -q
echo "OK: compose config 通过（env=${ENV_FILE}）"

if [[ "${CONFIG_ONLY}" -eq 1 ]]; then
  echo "== config-only 模式结束 =="
  exit 0
fi

if ! docker info >/dev/null 2>&1; then
  echo "[WARN] Docker daemon 不可用，跳过运行中容器巡检（已完成 config 校验）" >&2
  exit 0
fi

echo
echo "== 2) 运行中服务（只读） =="
"${COMPOSE[@]}" ps

QUEUE_CONTAINER="$("${COMPOSE[@]}" ps -q redis-queue 2>/dev/null || true)"
if [[ -z "${QUEUE_CONTAINER}" ]]; then
  echo "[WARN] redis-queue 未运行，跳过内存/队列巡检" >&2
  exit 0
fi

echo
echo "== 3) redis-queue 内存上限与策略（只读） =="
"${COMPOSE[@]}" exec -T redis-queue redis-cli INFO memory | tr -d '\r' | grep -E '^(used_memory_human|used_memory:|maxmemory_human|maxmemory:|mem_fragmentation_ratio)' || true
"${COMPOSE[@]}" exec -T redis-queue redis-cli CONFIG GET maxmemory maxmemory-policy appendfsync | tr -d '\r' || true

CONTAINER_LIMIT_BYTES="$(docker inspect --format '{{.HostConfig.Memory}}' "${QUEUE_CONTAINER}" 2>/dev/null || echo 0)"
USED_BYTES="$("${COMPOSE[@]}" exec -T redis-queue redis-cli INFO memory | tr -d '\r' | awk -F: '/^used_memory:/{print $2}')"
MAXMEMORY_BYTES="$("${COMPOSE[@]}" exec -T redis-queue redis-cli CONFIG GET maxmemory | tr -d '\r' | tail -n1)"

echo "container_mem_limit_bytes=${CONTAINER_LIMIT_BYTES} used_memory_bytes=${USED_BYTES:-unknown} maxmemory_bytes=${MAXMEMORY_BYTES:-unknown}"
if [[ "${MAXMEMORY_BYTES:-0}" =~ ^[0-9]+$ ]] && [[ "${MAXMEMORY_BYTES}" -gt 100663296 ]]; then
  echo "[WARN] maxmemory 高于 96MiB（100663296），与 160m 容器上限余量不足"
fi
if [[ "${CONTAINER_LIMIT_BYTES:-0}" =~ ^[0-9]+$ ]] && [[ "${MAXMEMORY_BYTES:-0}" =~ ^[0-9]+$ ]] && [[ "${CONTAINER_LIMIT_BYTES}" -gt 0 ]]; then
  HEADROOM=$((CONTAINER_LIMIT_BYTES - MAXMEMORY_BYTES))
  echo "maxmemory_headroom_bytes=${HEADROOM}（建议 >= 48MiB 留给进程/AOF rewrite）"
  if [[ "${HEADROOM}" -lt 50331648 ]]; then
    echo "[WARN] maxmemory 与容器上限余量 <48MiB"
  fi
fi
if [[ "${USED_BYTES:-0}" =~ ^[0-9]+$ ]] && [[ "${MAXMEMORY_BYTES:-0}" =~ ^[0-9]+$ ]] && [[ "${MAXMEMORY_BYTES}" -gt 0 ]]; then
  PERCENT=$((USED_BYTES * 100 / MAXMEMORY_BYTES))
  echo "used_memory_ratio=${PERCENT}%"
  if [[ "${PERCENT}" -ge 80 ]]; then
    echo "[WARN] 队列 Redis 使用率 >=80%，noeviction 下继续写入会失败；先扩容/清积压再调 maxmemory"
  fi
fi

echo
echo "== 4) Celery 队列积压（LLEN，只读；未创建的队列为 0） =="
for queue in default outbox scheduler channel sales tiktok_refresh; do
  length="$("${COMPOSE[@]}" exec -T redis-queue redis-cli LLEN "${queue}" 2>/dev/null | tr -d '\r' || echo "?")"
  printf 'queue=%-14s length=%s\n' "${queue}" "${length}"
done

echo
echo "== 5) Worker 执行位（只读；期望普通 worker=1，tiktok-refresh=1） =="
for service in worker tiktok-refresh-worker; do
  container="$("${COMPOSE[@]}" ps -q "${service}" 2>/dev/null || true)"
  if [[ -n "${container}" ]]; then
    echo "-- ${service} --"
    docker inspect --format 'name={{.Name}} image={{.Config.Image}} started={{.State.StartedAt}}' "${container}" || true
    "${COMPOSE[@]}" logs --tail=20 --no-color "${service}" 2>/dev/null | grep -E 'concurrency|celery@|ready|tiktok_refresh|tiktok-refresh' || true
  else
    echo "-- ${service} 未运行（tiktok-refresh-worker 需要 --profile tiktok-refresh）--"
  fi
done

echo
echo "巡检结束；本脚本未修改任何容器/配置。"
