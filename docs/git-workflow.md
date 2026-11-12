# Git 工作流（本地 + 服务器统一管理）

> 仓库：<https://github.com/shuaigemeng1-ui/erp.git>
> 默认分支：`main`。所有代码、文档、部署编排均通过该仓库管理；
> 服务器 `/opt/ventasync` 也是本仓库的一个 git 工作树，更新只走 `git pull`。

## 1. 仓库约定

| 项 | 约定 |
| --- | --- |
| 分支 | `main` 为唯一长期分支；功能/修复用短分支合并回 `main` |
| 提交信息 | `<type>(<scope>): <中文说明>`，type 用 feat/fix/chore/docs/deploy 等 |
| 发布标记 | 生产发布打 tag：`v1.0.0`、`v1.0.1`（回滚用 `git checkout <tag>`） |
| 敏感信息 | **禁止提交** `deploy/.env`、`backend/.env`、任何真实密码/密钥/Token；仓库里只允许 `.env.example`，且示例必须是假值 |
| Node 产物 | 服务器 2C2G 不跑 Node 构建，`frontend/dist` 纳入版本管理；**改前端后必须先 `npm run build` 再提交** |
| Python 产物 | 不提交 `__pycache__` / `.venv` / 测试缓存；后端镜像在服务器 Docker 内构建 |
| 数据库 | 不提交数据；MySQL 数据在 Docker 命名卷 `ventasync_mysql_data` |

## 2. 本地开发与推送

```bash
# 首次克隆
git clone https://github.com/shuaigemeng1-ui/erp.git
cd erp

# 日常开发
cd backend && python -m pytest tests/unit tests/contract -q && cd ..
cd frontend && npm run build && cd ..        # 改前端时必做，dist 需入库
git add -A
git commit -m "feat(module): 说明"
git push origin main
```

> 前端本地首次安装：`cd frontend && npm install`（`node_modules` 不入库）。

## 3. 服务器部署与更新（/opt/ventasync）

服务器首次已初始化为 git 工作树：

```bash
cd /opt/ventasync
git remote -v          # origin -> git@github.com:shuaigemeng1-ui/erp.git（SSH 部署密钥）
git status             # 应为 clean；deploy/.env、frontend/dist 等按规则处理
```

日常更新（推荐用脚本）：

```bash
cd /opt/ventasync
bash deploy/scripts/git_update.sh
```

脚本行为：`git pull --ff-only` → 使用 `docker-compose.yml + docker-compose.server.yml`
重新构建镜像 → `up -d` → 执行 release 迁移任务。等价手工命令：

```bash
cd /opt/ventasync && git pull --ff-only
cd deploy
docker compose -f docker-compose.yml -f docker-compose.server.yml --env-file .env build
docker compose -f docker-compose.yml -f docker-compose.server.yml --env-file .env up -d
docker compose -f docker-compose.yml -f docker-compose.server.yml --env-file .env --profile release run --rm migrate
```

部署前建议：确认 `/opt/backups` 有当日备份（每日 03:30 cron）；大版本前手动跑一次
`bash deploy/scripts/backup.sh --all-tenants -o /opt/backups`。

回滚：

```bash
cd /opt/ventasync
git fetch --tags
git checkout v1.0.0          # 或指定 commit
bash deploy/scripts/git_update.sh
```

> 服务器 remote 使用只读部署密钥（`/root/.ssh/erp_deploy`，已加入 GitHub Deploy keys）：
> ```bash
> git config core.sshCommand "ssh -i /root/.ssh/erp_deploy -o IdentitiesOnly=yes"
> ```
> HTTPS 拉取在国内网络下偶发 TLS 中断，SSH 更稳定；仓库改 private 也能继续拉取。

> 注意：
> * `deploy/.env` 不入库，`git checkout/pull` 不会覆盖服务器配置；
> * `frontend/dist` 已入库，服务器不需要 Node；
> * 不要把服务器 `.env`、`/root/ventasync-credentials.txt` 内容贴到仓库或聊天以外的公共位置；
> * 仓库当前为 **public**，推送前务必确认没有敏感信息。

## 4. 服务器 git 初始化记录（2026-11-12）

```bash
cd /opt/ventasync
git init
git remote add origin https://github.com/shuaigemeng1-ui/erp.git
git fetch origin main
git reset --hard origin/main       # deploy/.env 等未跟踪文件保留
git checkout -B main origin/main
```

旧的非 git 部署产物（`/opt/ventasync.tar.gz`、`/opt/deploy_server.sh`）已删除，
避免出现"两份代码"；需要回滚到 git 历史即可。
