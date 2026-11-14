# 百店 Webhook 压测、故障演练与上线验收工具

- 日期：2026-11-14
- 初始交接基线：`ceb608f`；本任务只新增 `scripts/` 与本文档，不修改
  `backend/app/modules/gateway`、`channels`、`sales` 等核心投递代码
- 交付：Webhook 压测、故障演练与上线验收工具（不修改核心投递代码）
- 关联：`docs/gateway-deployment.md`、`docs/ops-monitoring.md`、`deploy/gateway/README.md`

## 1. 目标与安全边界

验收目标：

- 单租户 **100 个店铺**，持续 **20 events/s × 30 分钟**；
- 95% 事件在 **2 分钟内**完成“Webhook → 订单写入”；
- 热店、重复事件、乱序、租户离线 30 分钟、Redis/Worker 中断、TikTok 429/部分订单返回；
- 结果可重复、测试数据可清理，不污染业务。

硬边界：

- **禁止**向生产 Gateway 发送会路由到真实店铺的模拟事件；那会产生模拟订单和库存变动。
- 默认 `--mode isolated`，仅用于本地/隔离环境。生产只允许
  `--mode production-unroutable`：使用 9 亿以上保留测试号段，发送后必须验证所有
  测试事件在 Gateway 中为 `unroutable`。
- 工具不提供“直接写订单/库存”的捷径；真实端到端指标必须来自测试租户库或测试 observer。
- `fault plan` 的 shell 命令只在隔离环境使用，必须显式 `--allow-shell-faults`。

## 2. 交付物

| 路径 | 说明 |
| --- | --- |
| `scripts/webhook_loadtest.py` | 可配置签名压测、429 重试、重复/乱序/部分订单、故障计划、Gateway SQLite 只读观测、报告、清理入口 |
| `scripts/webhook_loadtest_smoke.py` | 本地 smoke：启动当前 Gateway（真实应用）+ 临时 SQLite + 本地租户 sink，自动构造/中断/验证/清理 |
| `scripts/webhook_loadtest_fault_plan.example.json` | 租户离线/Redis/Worker 中断计划示例 |
| `scripts/reports/` | 运行时生成的 JSON/Markdown 报告（默认输出目录，不提交敏感数据） |
| 本文档 | 前置条件、命令、字段、验收清单和 smoke 证据 |

## 3. 签名与事件 payload

Gateway 公网入口：`POST /api/v1/gateway/webhooks/tiktok`

签名与当前 Gateway 一致：

```text
Authorization: <HMAC-SHA256(app_secret, app_key + raw_body) 的小写十六进制>
```

- raw body 必须与计算签名时完全一致；
- 也兼容 `Authorization: Bearer <hex>`（工具 `--auth-scheme bearer`）；
- Gateway 通过 `tiktok/production` 渠道应用配置的 `app_key/app_secret` 验签。

事件样例（工具会按 `--run-id` 生成唯一通知号与订单号）：

```json
{
  "tts_notification_id": "lt-20261114082501-0000001",
  "notification_id": "lt-20261114082501-0000001",
  "type": 1,
  "shop_id": "990000001",
  "timestamp": 1763100000,
  "data": {
    "order_id": "lt-20261114082501-O-0000000",
    "order_status": "AWAITING_SHIPMENT",
    "update_time": 1763100000,
    "line_items": [{"order_id": "lt-...-O-0000000", "sku_id": "SKU-LT-001-01", "quantity": 1}],
    "orders": [{"order_id": "lt-...-O-0000000", "order_status": "AWAITING_SHIPMENT"}],
    "_loadtest": {"run_id": "lt-...", "seq": 1, "out_of_order": false, "partial": false}
  }
}
```

- `type` 使用订单类事件（1/11/12 等）以便租户侧触发订单定向刷新；
- `_loadtest` 是测试标记，仅本工具生成；不依赖核心代码识别；
- `--partial-rate` 会让事件只携带部分订单/明细，用于观察下游“部分订单返回”的兼容行为；
- 重复发送使用同一个 raw body 与同一个 `tts_notification_id`，Gateway 靠唯一键合并并累加 `duplicate_count`。

## 4. 运行前置条件

1. **隔离/测试环境**：
   - 一个可写的测试租户（最好是一次性实例或可快照恢复的测试库）；
   - 该租户的 TikTok 店铺已在 Gateway 注册 `webhook-shops`；
   - Gateway 的 `GATEWAY_WEBHOOK_RELAY_SECRET` 与租户 `GATEWAY_WEBHOOK_RELAY_SECRET` 一致；
   - 测试租户的 worker/scheduler/Redis 可被停止/恢复（用于真实故障演练）。
2. **Python**：Python 3.12；安装 `backend/requirements.txt`（至少需要 `httpx`；
   租户库指标需要 `pymysql`）。
3. **租户 relay 回执**：当前 Gateway 只把租户 relay 的严格 JSON 回执
   `{"received": true}` 或 `{"duplicate": true}` 视为已交付；HTTP 200 但回执无效
   会继续重试。测试 sink/observer 必须模拟该回执；真实租户应通过其 relay 接口验证。
4. **只读观测**：
   - `--gateway-db` 指向 Gateway SQLite 文件（本地隔离可直接指向，生产请在 Gateway
     主机只读挂载或复制快照；工具用 `mode=ro` 打开）；
   - 真实端到端订单指标需要 `--tenant-db-dsn`（只读测试租户库）；否则只能报告
     Gateway→租户 relay 的 delivery 指标，**不能宣称订单写入完成**。
5. **凭据**：
   - `--app-key` / `--app-secret`：Gateway 上 TikTok 生产渠道应用凭据；
   - 不要写进命令行历史或提交到 Git，建议用环境变量/临时 shell 变量注入。

## 5. 本地 smoke（已执行）

### 5.1 100 店、20 events/s、10 秒（规模/分布 smoke）

```powershell
cd C:\Users\user\Desktop\ERP
python scripts/webhook_loadtest_smoke.py `
  --rate 20 --duration 10 --shops 100 --hot-shops 3 --hot-share 0.35 --outage-duration 0
```

### 5.2 5 店、5 events/s、10 秒 + 租户 sink 中断 3 秒（故障 smoke）

```powershell
python scripts/webhook_loadtest_smoke.py `
  --rate 5 --duration 10 --shops 5 --hot-shops 1 --hot-share 0.4 `
  --outage-start 4 --outage-duration 3
```

该命令会：

1. 启动当前工作区真实 Gateway（uvicorn，临时 SQLite）；
2. 通过 admin/instance API 注册临时实例和 `--shops` 个测试店铺；
3. 启动本地模拟租户 relay sink，校验 Gateway relay 签名、返回严格 JSON 回执、
   记录重复投递/部分订单；
4. 按参数制造 sink 中断，然后恢复；
5. 按配置发流量，输出 ACK/持久化/端到端/积压/重复报告；
6. 除非 `--keep`，自动删除临时 SQLite 和临时目录。**不写业务库，不连生产。**

## 6. 全量压测命令（隔离环境）

### 6.1 基线：100 店、20 events/s、30 分钟

```bash
export LOADTEST_APP_KEY='...'
export LOADTEST_APP_SECRET='...'
python scripts/webhook_loadtest.py run \
  --gateway-url http://127.0.0.1:8100 \
  --app-key "$LOADTEST_APP_KEY" \
  --app-secret "$LOADTEST_APP_SECRET" \
  --shops 100 --shop-id-base 990000000 \
  --rate 20 --duration 1800 --concurrency 64 \
  --hot-shops 3 --hot-share 0.35 \
  --duplicate-rate 0.02 --duplicate-delay-min 0.05 --duplicate-delay-max 0.8 \
  --out-of-order-rate 0.05 --partial-rate 0.05 \
  --gateway-db /srv/erp-gateway/gateway.db \
  --tenant-db-dsn 'mysql://loadtest_ro:...@127.0.0.1:3306/vs_tenant_loadtest' \
  --drain-timeout 600 \
  --output-dir scripts/reports
```

- 店铺号段必须在测试环境已注册；否则 Gateway 会记为 `unroutable`，不会产生业务写入。
- 100 店、20/s、30min = 36,000 个主事件；热店占比由 `--hot-shops/--hot-share` 控制。
- 报告默认生成 `scripts/reports/webhook-loadtest-<run_id>.{json,md}`。

### 6.2 租户离线 30 分钟 + Redis/Worker 中断

`scripts/webhook_loadtest_fault_plan.example.json` 提供结构；把 `<ISOLATED_ONLY>`
命令替换成目标隔离环境的 docker/compose 命令，并让 `--duration` 覆盖最后一个
恢复命令。例如：

```json
[
  {"at_seconds": 60, "label": "tenant-offline", "command": "ssh isolate 'cd /opt/ventasync && docker compose stop nginx'"},
  {"at_seconds": 1860, "label": "tenant-online", "command": "ssh isolate 'cd /opt/ventasync && docker compose start nginx'"},
  {"at_seconds": 1920, "label": "redis-stop", "command": "ssh isolate 'cd /opt/ventasync && docker compose stop redis'"},
  {"at_seconds": 1980, "label": "redis-start", "command": "ssh isolate 'cd /opt/ventasync && docker compose start redis'"},
  {"at_seconds": 2040, "label": "worker-stop", "command": "ssh isolate 'cd /opt/ventasync && docker compose stop worker'"},
  {"at_seconds": 2100, "label": "worker-start", "command": "ssh isolate 'cd /opt/ventasync && docker compose start worker'"}
]
```

```bash
python scripts/webhook_loadtest.py run \
  ... \
  --duration 2400 \
  --fault-plan scripts/webhook_loadtest_fault_plan.example.json \
  --allow-shell-faults \
  --gateway-db /srv/erp-gateway/gateway.db \
  --tenant-db-dsn 'mysql://loadtest_ro:...@.../vs_tenant_loadtest'
```

- 故障命令会按 `at_seconds` 执行；脚本要求 `--allow-shell-faults`，未提供时拒绝运行。
- 禁止把生产命令/凭据放入 fault plan；如无法提供隔离环境，保留为未验收项，不得用生产替代。
- 故障期间持续发送；恢复后看 Gateway `pending` 和租户订单的追平时间。

### 6.3 TikTok 429 / 部分订单返回

- 工具自身对 Gateway/代理返回的 **429** 会读取 `Retry-After` 并重试，报告中记录
  `retry_429_count`；其它 5xx/网络错误记录一次重试。
- “TikTok API 429/部分订单返回”属于租户 worker 调用上游的行为，不能由本客户端
  直接控制。隔离环境请用 TikTok 测试 double/代理注入 429 与部分订单响应，或使用
  TikTok 测试店铺；不要把生产 TikTok 当作故障注入环境。
- `--partial-rate` 会生成部分订单 payload，用于验证租户对部分订单的兼容处理；
  它不是对 TikTok API 响应本身的模拟。
- 该场景如果没有隔离 TikTok double，应在报告中列为“未执行”，不能用生产替代。

## 7. 生产 Gateway 的 `unroutable` 安全验证（仅在无隔离 Gateway 时）

生产 Gateway 可以接收未登记店铺的 Webhook（状态 `unroutable`，不转租户），可用于
只验证 Gateway 验签/持久化/ACK，但**必须**：

1. 使用 9 亿以上的保留测试号段（工具强制）；
2. `--gateway-db` 指向生产 Gateway SQLite 的只读副本或只读挂载；
3. 发送前工具用 `gateway_webhook_shops` 检查这些测试店铺**未绑定**；
4. 发送后工具检查所有测试事件状态都为 `unroutable`，否则退出失败；
5. 不注册测试店铺、不发送已登记店铺事件；不得产生订单/库存写入；
6. 仍建议先取得 Lead 对生产只读/测试号段的书面确认。

```bash
python scripts/webhook_loadtest.py run \
  --mode production-unroutable --allow-production-unroutable \
  --gateway-url https://gateway.example.com \
  --app-key "$LOADTEST_APP_KEY" --app-secret "$LOADTEST_APP_SECRET" \
  --shop-id-base 990000000 --shops 10 --rate 2 --duration 30 \
  --gateway-db /path/to/gateway.db \
  --drain-timeout 60 --output-dir scripts/reports
```

> 这是“不污染业务”的证明路径，不是全量 100 店/订单 E2E。全量订单链路必须在测试租户完成。

## 8. 指标与报告字段

`scripts/reports/webhook-loadtest-<run_id>.json` 与 `.md` 包含：

| 字段 | 含义 |
| --- | --- |
| `gateway_ack.latency.p99_ms` | 客户端 HTTP ACK P99（Gateway 返回 2xx） |
| `gateway_ack.success_rate` | ACK 成功率 |
| `gateway_ack.status_code_counts` | 状态码分布（含 429/401/5xx） |
| `persistence.rows` / `accepted_without_row` | Gateway SQLite 持久化行 / ACK 已接受但未落库 |
| `persistence.persist_delay` | 事件发送到 Gateway 持久化完成（SQLite `created_at`）的延迟 |
| `persistence.duplicate_count` | Gateway 唯一键合并的重复次数 |
| `observer_metrics.e2e` 或 `tenant_metrics.e2e` | 事件发送到测试 sink/测试租户订单写入的 P50/P95 |
| `delivery.delivery_latency` | Gateway 持久化到租户 relay ACK 的延迟（非订单写入） |
| `delivery.backlog.catchup_seconds` | 发送结束后到测试事件全部终结的时间 |
| `delivery.pending/dead/unroutable` | 最终状态分布 |
| `tenant_metrics.duplicate_inventory_movements` | 测试租户库中同一测试订单的重复 `order_deduct` 数 |
| `safety` | 模式、号段、`unroutable` 验证结果 |

**验收建议门槛（需 Lead 最终确认）**：

- ACK 成功率 ≥ 99.9%，ACK P99 ≤ 500 ms；
- `accepted_without_row = 0`；
- 95% 的测试租户订单在 120s 内出现（测试库 `orders.created_at`）；
- 恢复后积压追平 ≤ 120s，且 `pending` 不持续增长；
- 重复库存变动 = 0，重复事件不重复建单/扣库存；
- dead/unroutable 仅出现在预期故障/未注册店铺场景；
- 测试环境无跨实例路由、无生产写。

## 9. 上线顺序与逐档验收清单

每档至少先跑 **基线**，再跑 **热店/重复/乱序**；更高档再叠加故障。所有门槛以
Lead 审批后的 SLO 为准。

### 9.1 1 店（5 events/s，10 分钟）

- [ ] Gateway ACK 2xx 100%，P99 ≤ 500 ms；
- [ ] Gateway SQLite row 数 = ACK 数，无 `accepted_without_row`；
- [ ] 测试订单写入正常，重复事件不重复订单/库存；
- [ ] 停止/恢复测试 worker，积压可追平；
- [ ] 停止条件：任何数据丢失、重复库存、无法恢复积压。

### 9.2 10 店（10 events/s，20 分钟；1 热店）

- [ ] 10 个店的签名/路由均正确；
- [ ] 热店与普通店 backlog 不饥饿；
- [ ] 重复率、乱序率符合配置，Gateway `duplicate_count` 与重复发送数一致；
- [ ] E2E P95 ≤ 120s；
- [ ] 出现持续 pending 增长或单店积压 > 5min 时停止。

### 9.3 30 店（20 events/s，30 分钟）

- [ ] 基线 30 分钟无 5xx/无丢失；
- [ ] 租户离线/恢复（建议 10 分钟档）积压追平 ≤ 2min；
- [ ] Redis 中断 + Worker 中断各一次，恢复后无重复订单/库存；
- [ ] 429 测试 double 可重试并最终完成；不可重试错误有清晰日志；
- [ ] 监控：Gateway webhook pending、最老 pending 年龄、租户订单延迟、worker 队列、MySQL/Redis 连接、CPU/磁盘；
- [ ] 停止条件：ACK P99 超 SLO、持久化失败、积压不降、重复库存 >0、订单写入出错。

### 9.4 100 店（20 events/s，30 分钟 + 租户离线 30 分钟 + Redis/Worker 中断）

- [ ] 100 店签名/路由/顺序正确，热店不饿死其他店；
- [ ] 30 分钟稳定运行；95% 订单写入 ≤ 2min；
- [ ] 租户离线 30 分钟期间 Gateway 持久化不丢；恢复后积压追平 ≤ 2min；
- [ ] Redis/Worker 中断恢复无重复库存；
- [ ] 重复事件、乱序、部分订单返回不产生重复订单/库存；
- [ ] 生成报告并归档；监控无异常增长；
- [ ] 停止条件：任何业务数据污染、重复库存、丢失、不可恢复积压、P0 告警；
  命中任一条立即停止，保留现场并上报 Lead。

## 10. 清理测试数据

1. **本地 smoke**：默认自动删除临时 SQLite/日志/sink 数据，不产生业务数据。
2. **Gateway 测试事件**（隔离环境）：
   ```bash
   # 先 dry-run
   python scripts/webhook_loadtest.py cleanup \
     --gateway-db /srv/erp-gateway/gateway.db \
     --run-id lt-20261114082501
   # 确认行数/前缀后
   python scripts/webhook_loadtest.py cleanup \
     --gateway-db /srv/erp-gateway/gateway.db \
     --run-id lt-20261114082501 --apply
   ```
   仅按通知号前缀删除测试事件；`--include-audit` 才会同时清理包含 run_id 的审计记录。
   生产 Gateway 清理需 Lead 确认并先备份 SQLite。
3. **测试租户数据**：不要在生产库按订单号“局部删订单/库存流水”后就地继续写，可能破坏库存余额。
   测试必须使用一次性租户或可恢复快照；压测结束后恢复测试库快照（首选），
   或在独立测试库中按 `orders.external_order_no LIKE 'lt-<run_id>-%'` 清理由业务负责人确认的数据。
   工具不提供生产租户库自动删数据。
4. 测试店铺映射：隔离环境可在 Gateway 管理 API/实例 API 删除 `webhook-shops`；
   生产不得保留测试店铺映射。

## 11. 本次 smoke 实测（2026-11-14）

环境：Windows 本地隔离；当前工作区真实 Gateway（uvicorn + 临时 SQLite，含当次
Gateway 改动）+ 本地 mock 租户 relay sink；无 MySQL/Redis/生产网络。

### 11.1 100 店、20 events/s、10 秒

| 指标 | 结果 |
| --- | --- |
| 主事件 / 重复发送 | 200 / 15（实际速率 20.13 events/s） |
| Gateway ACK | 200/200，P50 29.17ms，P95 104.81ms，**P99 205.59ms** |
| 持久化 | 200 行，`accepted_without_row=0`，`duplicate_count=15` |
| 最终状态 | delivered 200，pending/dead/unroutable 0 |
| Gateway→sink E2E（observer） | P50 186.94ms，P95 712.48ms |
| 积压追平 | 0.006s（无 timeout） |
| 重复投递/重复库存 | sink duplicate deliveries 0；observer duplicate inventory 0 |
| 部分订单 / 乱序 / 热店事件 | 26 / 4 / 62 |
| 签名失败 | 0 |

### 11.2 5 店、5 events/s、10 秒 + sink 中断 3 秒

| 指标 | 结果 |
| --- | --- |
| 主事件 / 重复发送 | 50 / 2 |
| Gateway ACK | 50/50，P50 20.81ms，P95 44.08ms，**P99 69.55ms** |
| 持久化 | 50 行，`accepted_without_row=0`，`duplicate_count=2` |
| 最终状态 | delivered 50，pending/dead/unroutable 0 |
| Gateway→sink E2E（observer） | P50 868.58ms，P95 8036.79ms（含 3s sink 中断及重试退避） |
| 积压追平 | 4.06s（无 timeout） |
| 重复投递/重复库存 | sink duplicate deliveries 0；observer duplicate inventory 0 |
| 部分订单事件 | 14 |
| 签名失败 | 0 |
| 故障 | sink 中断 3s，15 次 503 尝试后恢复；无数据丢失 |

**这组数字只证明工具与本地真实 Gateway 验签/持久化/relay/积压路径可运行**；
不代表 100 店 30 分钟生产验收，也不代表真实订单写入（真实订单需 `--tenant-db-dsn`
或测试 observer 提供）。截图未生成；以本表和 JSON 报告为准。

## 12. 已知限制与未完成项

1. 未在隔离测试租户库执行真实订单写入/库存变动（本次 smoke 用本地 sink）；
   真实 E2E 需提供测试租户 MySQL 只读 DSN，且工具只读不写。
2. 未实际执行 100 店/20 events/s/30 分钟、30 分钟租户离线和 Redis/Worker 中断；
   这些需要独立压测环境、控制台命令与 Lead 授权，不能对生产做故障注入。
3. 真实 TikTok 429/部分订单返回需要 TikTok 测试 double/测试店铺；工具只能生成
   部分订单 payload，不能替代上游故障注入。
4. 生产 `unroutable` 路径尚未对真实生产 Gateway 执行；如无隔离环境，需 Lead 批准、
   生产只读 Gateway DB 与测试号段。
5. 当前 smoke 未覆盖 Gateway SQLite 备份/恢复与多副本分发，需在正式环境补充。
