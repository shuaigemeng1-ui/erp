# ERP 多租户 SaaS（FastAPI + Vue 3 + MySQL）

仓库：<https://github.com/shuaigemeng1-ui/erp.git>（`main` 分支；本地与服务器统一用 git 管理，见 [docs/git-workflow.md](docs/git-workflow.md)）。

依据仓库根目录 `plan.txt` 建设的全新系统，与桌面 `ventasync` Laravel 项目相互独立：
`ventasync` 只作为业务参考（Shopee 订单 / 库存扣减 / 状态映射），本目录不复用其代码、不改动其文件。

## 目录

```text
backend/    FastAPI + SQLAlchemy + Alembic（中心库 / 租户库双迁移）
frontend/   Vue 3 + TypeScript + Vite + Element Plus
deploy/     Docker Compose、Nginx、迁移发布任务、备份恢复脚本
docs/       架构、API 契约、数据模型、验收说明
plan.txt    需求与实施计划（原样保留）
```

## TikTok Shop 接入（Phase 1 已完成）

- 平台管理 → **渠道应用** → 选择 **TikTok Shop**，填写 `App Key`、`App Secret` 与回调地址：
  `https://erp.alienyan.store/api/v1/integrations/tiktok/oauth/callback`（需加入 TikTok 应用白名单）
- 租户 → **店铺连接** → **授权 TikTok Shop**，完成 OAuth 后店铺与加密凭据自动入库
- 已实现：OAuth 授权、token 换取/刷新、HMAC-SHA256 签名客户端、店铺绑定、`shop_cipher` 加密保存
- Phase 2 已完成：订单拉取/落库、商品同步与映射、库存推送；回调 Webhook、退货/结算/商品发布后续补

## 登录入口（租户与平台管理已隔离）

| 入口 | 地址 | 说明 |
| --- | --- | --- |
| 租户 ERP | `/login` | 租户成员/负责人登录；只显示商品、库存、店铺、订单、成员等 ERP 菜单 |
| 平台管理 | `/platform/login` | 仅平台管理员登录；只显示租户管理、套餐、渠道应用、平台审计 |

* 平台管理员用租户入口登录后，只进入租户 ERP，不再显示任何平台管理菜单/入口；
* 平台控制台需从 `/platform/login` 单独登录，进入后也不会显示租户 ERP 菜单；
* 后端会话带 `scope`（tenant / platform），跨端调用接口返回 `403 SCOPE_DENIED`。

## 首版范围

登录、租户开通、成员与角色、数据范围、主商品及 SKU、Shopee 店铺授权、平台商品读取与映射、
订单同步、库存变化、库存推送、任务记录、审计。

## 本地开发

后端（不自动迁移、不自动连接生产）：

```bash
cd backend
python -m pip install -r requirements-dev.txt
cp .env.example .env          # 修改数据库 / Redis 连接
python -m alembic -c alembic/center.ini upgrade head
python -m alembic -c alembic/tenant.ini -x db_url=mysql+pymysql://user:pass@127.0.0.1:3306/vs_tenant_1 upgrade head
python -m uvicorn app.main:app --reload --port 8000
```

前端：

```bash
cd frontend
npm install
npm run dev
npm run build     # 提交前必须通过
```

部署见 `deploy/README.md`。验收项与证据见 `docs/acceptance.md`，逐条计划对照见 `docs/plan-mapping.md`。

## 已部署实例（2026-11-11）

真实服务器部署（Ubuntu 24.04 / 2C2G / Docker Compose / Caddy 反代）已完成并通过端到端验证：

* 访问地址：**https://erp.alienyan.store/**（Let's Encrypt 证书自动续期）
* 只允许域名访问：HTTP IP 301 跳转域名、HTTPS IP 无法握手、Host≠SNI 返回 421；`TRUSTED_HOSTS` 限制后端 Host
* 平台管理员凭据保存在服务器 `/root/ventasync-credentials.txt`（600 权限）
* 架构与步骤：`docs/real-deployment.md`
* 服务器代码目录 `/opt/ventasync` 为 git 工作树，更新执行 `bash deploy/scripts/git_update.sh`
* 已验证：双租户真实开通（各 29 表、0 外键）、邀请激活、权限、商品/库存、租户隔离、暂停/启用、登录限流、备份脚本
* P0 已上线：域名 + HTTPS（自动续期）、管理员/MySQL/中心库密码轮换、每日 03:30 全量备份、单租户恢复演练
* 待外部条件：Shopee 应用与真实测试店铺联调（需 Partner ID/Key 与店铺授权）；详见 `docs/real-deployment.md` §8


## 当前交付状态（2026-11-11 离线验证）

| 项 | 结果 |
| --- | --- |
| 后端应用入口 `app.main:app` | 可导入；67 条路由 / 55 个 OpenAPI 路径 |
| 后端单元 + 契约测试 | `pytest tests/unit tests/contract -q` → 253 passed |
| 契约自动校验 | `python scripts/verify_contracts.py` → 36 项全通过 |
| 中心库 / 租户库迁移 | 15 / 29 张表；`FOREIGN KEY` 0；表与字段中文注释齐全 |
| 前端生产构建 | `npm run build` 通过（vue-tsc + vite，dist ≈ 1.5 MB） |
| 部署编排 | `docker compose config -q` 通过（未启动容器） |

真实环境验收（MySQL 8.4 / Redis / Shopee 授权店铺 / 备份恢复演练 / HTTPS）需在部署机执行，
离线结果不替代，详见 `docs/acceptance.md` §5。

## 硬性约束

* 所有业务表禁止外键（模型、迁移、手写 SQL 均不得出现 `FOREIGN KEY`）。
* 表和字段必须有中文注释；中心库与租户库均需通过 `REFERENTIAL_CONSTRAINTS` 零外键验收。
* 金额用 `DECIMAL` + `Decimal`；时间存 UTC；大整数 ID 以字符串返回前端。
* 迁移由独立发布任务执行，API 不在启动时自动迁移。
* 不擅自启动前后端项目或连接生产环境。

### TikTok Shop「先建店铺、后授权」流程

1. 店铺连接 → **新建店铺**：选择渠道（TikTok Shop / Shopee）+ 填写 ERP 显示名称；
2. 创建后状态为 **待授权**，不会参与订单 / 库存同步；
3. 操作栏点击 **复制授权链接**：
   - 后端用平台应用 `App ID` 作为 TikTok `service_id`，拼接
     `https://services.tiktokshop.com/open/authorize?service_id=...&state=...`
     （US 应用使用 `services.us.tiktokshop.com`）；
   - 链接绑定该 ERP 店铺的一次性 state，可直接复制给店铺主账号在任意浏览器打开；
4. 店铺主账号授权后，TikTok 回调到
   `/api/v1/integrations/tiktok/oauth/callback`，后端换取 token、读取 shops，
   把原「待授权」店铺更新为 **已连接**，并保存加密凭据。

