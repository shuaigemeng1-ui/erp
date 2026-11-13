# 部署形态

同一套代码支持三种运行形态，由环境变量 `DEPLOYMENT_MODE` 控制，默认 `center`：

| 模式 | 用途 | 服务端限制 |
|---|---|---|
| `center` | 统一 ERP 管理中心 | 平台管理、租户业务与 Agent 接口均可用 |
| `tenant` | 租户独立服务器实例 | 关闭 `/api/v1/platform/*`，仅保留租户业务与本机 Agent |
| `gateway` | 独立集成网关 | 仅保留 `/api/v1/gateway/*` 与健康检查 |

可访问 `GET /api/v1/runtime` 查看当前模式：

```json
{"ok": true, "mode": "center", "app": "..."}
```

独立网关健康检查：`GET /api/v1/gateway/health`。

当前中心已按 `center` 模式部署；租户独立实例的数据库、备份与接入模板已在批次 2/3/4 交付，
账号仍以中心库账号/成员关系为唯一认证来源；独立网关与本地认证仍按后续批次推进。

## 独立租户实例接入（批次 4）

- 接入手册：`docs/batch4-tenant-onboarding.md`
- 模板目录：`deploy/scripts/tenant-onboarding/`（config/install/provision/migrate/copy_data/reconcile/verify_isolation）
- 固定绑定：`TENANT_ID`、`INSTANCE_ID` 与 `ERP_WORKER_TENANT_ID`/`ERP_WORKER_INSTANCE_ID` 必须一致，
  `ERP_WORKER_STRICT_BINDING=true`；任一时刻同一租户只能有一个写入实例。
- 迁移白名单：`deploy/scripts/tenant-onboarding/migration-whitelist.json`（保留 `id`/`tenant_id`/`member_no`；
  平台密钥表/列禁止迁移）。
- 所有模板默认 plan；`--apply` 只能在新租户实例、维护窗口、Lead 审批后执行，禁止在中心生产机运行。
