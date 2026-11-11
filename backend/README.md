# 后端（FastAPI + SQLAlchemy 2 + MySQL）

## 结构

```text
app/
  core/     配置、日志、错误码、权限编码、安全（bcrypt/AES/令牌）、Redis、请求上下文、权限计算
  db/       Base/MetaData、中心库会话、租户库 Engine 缓存与会话、模型注册表
  api/      deps（登录用户/租户上下文/权限依赖）、router 汇总
  modules/  identity access platform catalog inventory channels sales tasks audit
  workers/  Celery 应用、独立调度进程
alembic/
  center/   中心库迁移（alembic -c alembic/center.ini）
  tenant/   租户库迁移（alembic -c alembic/tenant.ini -x db_url=...）
scripts/    契约校验脚本
tests/      unit（纯逻辑） / contract（API 契约）
```

分层：`router → service → repository`。事务由 Service 管理，Repository 只查询；路由不做业务判断。
接口使用同步 SQLAlchemy Session；阻塞数据库的接口用普通 `def`，每个请求/任务独立创建、关闭 Session。

## 常用命令（在 backend/ 目录执行）

```bash
python -m pip install -r requirements-dev.txt
cp .env.example .env

# 迁移（独立发布任务执行，API 启动时不迁移）
python -m alembic -c alembic/center.ini upgrade head
python -m alembic -c alembic/tenant.ini -x db_url=mysql+pymysql://user:pass@127.0.0.1:3306/vs_tenant_1 upgrade head

# 首次初始化平台管理员（不开放公共注册）
python -m app.cli platform-init

# 本地开发（仅本地）
python -m uvicorn app.main:app --reload --port 8000

# 质量检查
python -m compileall -q app
python -m pytest tests/unit -q
python scripts/verify_contracts.py
```

## 硬性规范

* 模型、迁移、手写 SQL 一律禁止外键；关联只存 ID 列并按查询建索引。
* 所有表与字段必须有中文注释；唯一性用唯一约束表达。
* 时间存 UTC（naive DATETIME），按租户时区展示；金额用 `Decimal`/`DECIMAL`。
* 大整数 ID 在 JSON 中序列化为字符串。
* 租户库连接只来自中心库 `tenant_databases` 受控映射，凭据 AES 加密；Engine 按「租户 + 连接版本」缓存。
* 租户上下文放入 ContextVar（请求/任务级），不放模块级全局变量；未建上下文时业务查询直接失败。
* 外部平台请求在数据库事务提交后执行；库存推送按店铺+SKU 串行，执行时读最新库存。
* 手动任务执行前复核成员权限；定时任务使用受限自动化身份。
* Redis 缓存与任务队列分开配置；清缓存不能清除待执行任务。

## 模块负责人与写作用域

见 `docs/architecture.md`。共享层（core/db/api/main）由 Lead 维护，其他模块只通过文档中的固定契约调用。
