#!/usr/bin/env bash
# 租户实例本地备份脚本：MySQL 全库 + 保留 30 天。
# 由 Agent 备份任务调用，也可手工执行：bash deploy/backup.sh
set -euo pipefail

cd "$(dirname "$0")"
set -a
. ./.env
set +a

BACKUP_ROOT="${BACKUP_ROOT:-/opt/ventasync/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_ROOT}/${STAMP}"
mkdir -p "$TARGET"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.server.yml --env-file .env)
MYSQL_SERVICE="${MYSQL_SERVICE:-mysql}"
ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD 未配置}"

CENTER_DB="${CENTER_DB_NAME:-ventasync_center}"
DBS=$("${COMPOSE[@]}" exec -T "$MYSQL_SERVICE" mysql -uroot -p"$ROOT_PASSWORD" -N -e \
  "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'ventasync_%';" 2>/dev/null)
TENANT_DBS=$("${COMPOSE[@]}" exec -T "$MYSQL_SERVICE" mysql -uroot -p"$ROOT_PASSWORD" -N -e \
  "SELECT database_name FROM ${CENTER_DB}.tenant_databases WHERE status='ready';" 2>/dev/null || true)
DBS=$(printf '%s\n%s\n' "$DBS" "$TENANT_DBS" | awk 'NF' | sort -u)

for db in $DBS; do
  "${COMPOSE[@]}" exec -T "$MYSQL_SERVICE" mysqldump -uroot -p"$ROOT_PASSWORD" \
    --single-transaction --quick --routines --events --triggers "$db" \
    | gzip > "${TARGET}/${db}.sql.gz"
done

# 配置与编排文件一并备份（不含密钥文件内容以外的敏感数据）
cp .env "$TARGET/env.backup"
cp docker-compose.yml "$TARGET/docker-compose.yml"
cp docker-compose.server.yml "$TARGET/docker-compose.server.yml"

SIZE=$(du -sb "$TARGET" | awk '{print $1}')
KEY="db:${STAMP}"
echo "BACKUP_KEY=${KEY}"
echo "BACKUP_SIZE=${SIZE}"
echo "BACKUP_DIR=${TARGET}"

find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" -exec rm -rf {} +
