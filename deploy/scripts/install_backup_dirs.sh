#!/usr/bin/env bash
# =============================================================================
# 备份目录与密钥权限初始化（root 执行一次即可，可重复执行）
#
# 背景：Agent 的执行进程用户是 erp-agent-exec（共享组 erp-agent），
# 升级前备份与每日定时备份都以该用户运行。默认备份根目录
# /var/lib/ventasync-backups 与密钥目录 /etc/ventasync 由 root 创建并授权，
# 否则升级前备份会因权限失败而中止升级（预期保护）。
#
# 用法：
#   sudo bash deploy/scripts/install_backup_dirs.sh
#   sudo BACKUP_ROOT=/data/ventasync-backups BACKUP_GROUP=erp-agent \
#        bash deploy/scripts/install_backup_dirs.sh
#
# 完成后目录/文件约定：
#   /var/lib/ventasync-backups          2770 root:erp-agent
#   /var/lib/ventasync-backups/releases 2770 root:erp-agent
#   /var/lib/ventasync-backups/offsite  2770 root:erp-agent
#   /etc/ventasync                      2770 root:erp-agent
#   /etc/ventasync/backup.key           640 root:erp-agent（缺失时自动生成）
#   /etc/tmpfiles.d/ventasync-backups.conf（开机保持权限）
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -f "${DEPLOY_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${DEPLOY_DIR}/.env"
    set +a
fi

: "${BACKUP_ROOT:=/var/lib/ventasync-backups}"
: "${BACKUP_GROUP:=erp-agent}"
: "${BACKUP_ENCRYPTION_KEY_FILE:=/etc/ventasync/backup.key}"
KEY_DIR="$(dirname "${BACKUP_ENCRYPTION_KEY_FILE}")"

log() { printf '[backup-init] %s\n' "$*"; }
warn() { printf '[backup-init] 警告：%s\n' "$*" >&2; }
die() { printf '[backup-init] 错误：%s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "请用 root 执行：sudo bash deploy/scripts/install_backup_dirs.sh"
command -v openssl >/dev/null 2>&1 || die "未找到 openssl（生成备份密钥必需）"

# ---------------------------------------------------------------- 组与用户
if ! getent group "${BACKUP_GROUP}" >/dev/null 2>&1; then
    log "创建共享组：${BACKUP_GROUP}"
    groupadd --system "${BACKUP_GROUP}"
fi
for user in erp-agent erp-agent-exec; do
    if id "${user}" >/dev/null 2>&1; then
        if ! id -nG "${user}" | tr ' ' '\n' | grep -Fxq "${BACKUP_GROUP}"; then
            log "将 ${user} 加入组 ${BACKUP_GROUP}"
            usermod -aG "${BACKUP_GROUP}" "${user}"
        fi
    else
        warn "用户 ${user} 不存在（Agent 安装脚本会创建）；组 ${BACKUP_GROUP} 已就绪，安装后请重跑本脚本"
    fi
done

# ---------------------------------------------------------------- 目录与权限
for dir in "${BACKUP_ROOT}" "${BACKUP_ROOT}/releases" "${BACKUP_ROOT}/offsite" "${KEY_DIR}"; do
    install -d -m 2770 -o root -g "${BACKUP_GROUP}" "${dir}"
    chmod 2770 "${dir}"
    chown "root:${BACKUP_GROUP}" "${dir}"
    log "目录就绪：${dir} $(stat -c '%a %U:%G' "${dir}")"
done

# tmpfiles：重启后保持属组与权限（与目录现状一致）
TMPFILES_SRC="${DEPLOY_DIR}/systemd/ventasync-backups.tmpfiles.conf"
if [[ -f "${TMPFILES_SRC}" ]]; then
    install -m 644 "${TMPFILES_SRC}" /etc/tmpfiles.d/ventasync-backups.conf
    systemd-tmpfiles --create /etc/tmpfiles.d/ventasync-backups.conf 2>/dev/null || \
        warn "systemd-tmpfiles 执行失败（不影响当前权限，重启后由 tmpfiles 再应用）"
    log "已安装 tmpfiles 配置：/etc/tmpfiles.d/ventasync-backups.conf"
fi

# ---------------------------------------------------------------- 加密密钥
if [[ ! -s "${BACKUP_ENCRYPTION_KEY_FILE}" ]]; then
    ( umask 027; openssl rand -base64 32 > "${BACKUP_ENCRYPTION_KEY_FILE}" )
    chown "root:${BACKUP_GROUP}" "${BACKUP_ENCRYPTION_KEY_FILE}"
    chmod 640 "${BACKUP_ENCRYPTION_KEY_FILE}"
    log "已生成备份加密密钥：${BACKUP_ENCRYPTION_KEY_FILE}（请异地妥善保管，丢失异地包不可解密）"
else
    chown "root:${BACKUP_GROUP}" "${BACKUP_ENCRYPTION_KEY_FILE}" 2>/dev/null || true
    chmod 640 "${BACKUP_ENCRYPTION_KEY_FILE}" 2>/dev/null || true
    log "备份加密密钥已存在：${BACKUP_ENCRYPTION_KEY_FILE}"
fi

# ---------------------------------------------------------------- Docker 权限提示
if id erp-agent-exec >/dev/null 2>&1; then
    if ! id -nG erp-agent-exec | tr ' ' '\n' | grep -Fxq docker; then
        warn "erp-agent-exec 不在 docker 组，备份脚本无法调用 docker；请确认 Agent 安装脚本已处理"
    fi
fi

# ---------------------------------------------------------------- Docker Compose 版本
if command -v docker >/dev/null 2>&1; then
    docker compose version >/dev/null 2>&1 || warn "docker compose v2 不可用，备份脚本需要它"
else
    warn "未找到 docker 命令，备份脚本需要 docker"
fi

cat <<EOF

[backup-init] 完成。验证：
  sudo -u erp-agent-exec test -w "${BACKUP_ROOT}" && echo "备份目录可写：OK"
  sudo -u erp-agent-exec test -r "${BACKUP_ENCRYPTION_KEY_FILE}" && echo "密钥可读：OK"
  stat -c '%a %U:%G %n' "${BACKUP_ROOT}" "${BACKUP_ROOT}/releases" "${KEY_DIR}" "${BACKUP_ENCRYPTION_KEY_FILE}"

之后以 Agent 用户验证一次备份（不写中心库）：
  sudo -u erp-agent-exec bash ${DEPLOY_DIR}/scripts/backup.sh --trigger manual --no-record --no-offsite
EOF
