# VentaSync 多租户 SaaS（FastAPI + Vue 3 + MySQL）

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

部署见 `deploy/README.md`。验收项与证据见 `docs/acceptance.md`。

## 硬性约束

* 所有业务表禁止外键（模型、迁移、手写 SQL 均不得出现 `FOREIGN KEY`）。
* 表和字段必须有中文注释；中心库与租户库均需通过 `REFERENTIAL_CONSTRAINTS` 零外键验收。
* 金额用 `DECIMAL` + `Decimal`；时间存 UTC；大整数 ID 以字符串返回前端。
* 迁移由独立发布任务执行，API 不在启动时自动迁移。
* 不擅自启动前后端项目或连接生产环境。
