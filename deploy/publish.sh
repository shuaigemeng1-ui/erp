#!/usr/bin/env bash
# 发布当前 Git 提交：构建、推送镜像、更新中心版本并自动登记发布版本。
# 用法（在中心服务器仓库根目录）：bash deploy/publish.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="$(git rev-parse --short=12 HEAD)"
REGISTRY="${REGISTRY:-erp.alienyan.store}"
IMAGE_PREFIX="${REGISTRY%/}/"
cd deploy
set -a; . ./.env; set +a
export APP_IMAGE_TAG="$VERSION"
export IMAGE_REGISTRY="$IMAGE_PREFIX"
export APP_VERSION="$VERSION"

echo "==> 发布版本 $VERSION"
docker compose -f docker-compose.yml -f docker-compose.server.yml --env-file .env build api nginx
# 兼容 compose 构建时未带前缀的镜像标签
docker tag "ventasync-backend:${VERSION}" "${IMAGE_PREFIX}ventasync-backend:${VERSION}" 2>/dev/null || true
docker tag "ventasync-frontend:${VERSION}" "${IMAGE_PREFIX}ventasync-frontend:${VERSION}" 2>/dev/null || true
# 租户升级由 Agent 在租户服务器 git pull + docker compose build，无需分发镜像包。

# 持久化中心版本与镜像仓库前缀
python3 - "$VERSION" "$IMAGE_PREFIX" <<'PY'
from pathlib import Path
import sys
version, prefix = sys.argv[1], sys.argv[2]
path = Path('.env')
lines = path.read_text(encoding='utf-8').splitlines()
keys = {'APP_IMAGE_TAG': version, 'APP_VERSION': version, 'IMAGE_REGISTRY': prefix}
out = [line for line in lines if line.split('=', 1)[0] not in keys]
out.extend(f'{key}={value}' for key, value in keys.items())
path.write_text('\n'.join(out) + '\n', encoding='utf-8')
PY

docker compose -f docker-compose.yml -f docker-compose.server.yml --env-file .env up -d api worker scheduler nginx
echo "==> 完成：$VERSION"
echo "租户实例需要设置 IMAGE_REGISTRY=${IMAGE_PREFIX} 并点击升级到 ${VERSION}"
