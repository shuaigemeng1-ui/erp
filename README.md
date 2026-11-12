# ERP 多租户 SaaS

FastAPI + Vue 3 + MySQL，中心库 + 每租户独立业务库。桌面 `ventasync` Laravel 项目仅作为业务参考。

## 当前实现

- 平台创建租户、邀请负责人、全局账号加入多个租户。
- 多角色、店铺/库存地点数据范围、敏感字段过滤、操作审计。
- SPU—SKU商品中心：中英文资料、供应商/人员、尺寸、SKU多别名、三类成本/重量及销售状态汇总。
- SPU详情的基本信息、销售信息和字段操作日志；SKU别名贯通TikTok商品、订单与库存识别。
- 商品图片持久化上传、基础字典、版本校验，以及库存余额与流水。
- Shopee、TikTok 店铺授权、现有商品读取及映射、订单同步、库存推送。
- 持久化任务、Outbox、独立 Worker 和调度器，权限与同步开关在执行时复核。
- 可复制、可撤销的长期授权入口；打开入口后生成短期一次性 OAuth state。

本轮实现和验证结果见 [验收记录](docs/acceptance.md)。既有线上部署见 [生产记录](docs/real-deployment.md)；本轮SPU商品中心增量尚未发布，真实平台试点优先TikTok。在线商品调价、促销和完整刊登属于后续阶段。

## 目录与文档

| 目录 | 内容 |
| --- | --- |
| `backend` | API、业务服务、Worker、中心库与租户库迁移、回归测试 |
| `frontend` | Vue 3 管理后台 |
| `deploy` | Docker Compose、Nginx、迁移发布、备份恢复 |
| `docs` | 架构、数据模型、API 契约、验收记录 |

- [架构与边界](docs/architecture.md)
- [数据模型及完整迁移链](docs/data-model.md)
- [API 契约](docs/api-contract.md)
- [原计划对照](docs/plan-mapping.md)
- [部署方法](deploy/README.md)
- [历史部署记录](docs/real-deployment.md)：历史记录不等于本轮已部署或复验。

## 开发与发布

后端安装 `backend/requirements-dev.txt`，配置自己的 `.env`。中心库与租户库迁移由独立发布任务执行，API 启动不自动迁移。迁移版本以 Alembic 实际版本图为准；当前中心库 `center_0006`、租户库 `tenant_0011`，共17个迁移文件，业务表为中心15张/租户32张。

本地后端入口保持 `uvicorn app.main:app --host 0.0.0.0 --port 8000`，前端在 `frontend` 执行 `npm run dev`；这些是手动启动说明，智能体不得擅自启动项目。

提交前执行相关后端回归、`python scripts/verify_contracts.py` 和前端 `npm run build`。`frontend/dist` 是受版本管理的服务器发布产物；仅审阅构建时可指定临时输出目录，正式发布应更新受管理产物。

## 硬性约束与待验收

- 所有应用表禁止外键，表与字段必须有中文注释；ALTER 修改后的最终模式也必须保留注释。
- 金额采用 DECIMAL/Decimal，时间存 UTC，大整数 ID 以 JSON 字符串输出。
- 首次真实推送保持关闭，在测试店铺和 SKU 映射核对完成后显式开启。
- 真 MySQL 并发、Worker 故障恢复、TikTok/Shopee 真店闭环及本轮迁移后的备份恢复仍需独立授权环境验收。
- 成员、店铺、商品/SKU 已接入套餐额度执行，真实MySQL并发验收待完成；图片首期使用持久化文件卷，S3/导出、在线支付、完整刊登及售后留待后续阶段。
- SKU当前/指导/平均成本本轮人工维护；SPU只汇总。别名租户内唯一，停用后保留归属；捆绑与组装类型尚不包含BOM或组装扣库存。
