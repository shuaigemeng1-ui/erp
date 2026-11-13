#!/usr/bin/env bash
# 租户实例备份入口（保持旧路径兼容：Agent 备份任务、升级前备份、手工执行都调用本文件）。
#
# - Agent 的 backup 任务与 upgrade 步骤会执行 `deploy/backup.sh`，从输出解析
#   BACKUP_KEY / BACKUP_SIZE 上报中心；
# - 默认触发来源 scheduled；升级前备份可通过 BACKUP_TRIGGER=pre_upgrade 覆盖；
# - 实际实现见 deploy/scripts/backup.sh（目录 700/600、加密、异地、清单、记录）。
set -euo pipefail
cd "$(dirname "$0")"

TRIGGER="${BACKUP_TRIGGER:-scheduled}"
exec bash scripts/backup.sh --trigger "${TRIGGER}" "$@"
