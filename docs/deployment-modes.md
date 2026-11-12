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

当前中心已按 `center` 模式部署；租户实例和网关的独立数据库、本地认证与渠道密钥迁移仍在后续阶段实现。
