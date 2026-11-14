# Task-47 百店 20/s 长稳与 30 分钟离线演练记录

- 日期：2026-11-14
- 场景：100 店、20 events/s、60 分钟连续流入（前 30 分钟正常，后 30 分钟租户离线），本地临时 Gateway SQLite + 本地 sink
- 安全边界：**未连接生产、未启动生产容器、未向生产写事件**；Gateway/sink 均为 127.0.0.1 临时进程
- 状态：**按 Lead 要求提前停止长跑，未完成最终 drain/60 分钟追平验收；以下只报告已落盘证据**

## 1. 结论摘要

| 验收项 | 结果 | 判定 |
| --- | --- | --- |
| 100 店 20/s 持续流入 | 数据库记录 **72,000** 事件、100 个店铺、首事件 08:56:36Z、末事件 09:59:26Z | 通过（流量/持久化覆盖） |
| Gateway ACK P99 ≤1s | **全量 ACK 未取得**：长跑被提前中止，主压测脚本未输出最终 JSON | **未证实**（见第 5 节 smoke 参考） |
| 95% E2E ≤120s | 正常基线 `created_at→delivered_at` P95 **8.04s**；但这是 Gateway→relay，不是订单写入 | 基线 relay 部分通过；订单写入未验证 |
| 逐事件无遗漏 | 72,000 行中 delivered **47,660**、pending **24,340**、dead/unroutable **0** | **未通过（未完成 drain）** |
| 重复库存 = 0 | 本地无租户订单库，最终 sink summary 未保留；无法证明 | **未验证** |
| 追平 ≥30/s 且 ≤60min | 默认配置追平约 **3.9/s**；测试性提高 Gateway 并发后短时约 **34.7/s**；最终仍有 24,340 pending | **未通过**（见第 4 节） |

**总判定：不通过/未完成。** 本次可确认 100 店 20/s 的 Gateway 接收/持久化覆盖和 30 分钟租户离线故障窗口；不能确认最终追平和订单/库存侧验收。

## 2. 原始证据文件

目录：`scripts/reports/task47/lt-task47-20261114085624/`

| 文件 | 说明 |
| --- | --- |
| `raw_metrics.json` | 从本次临时 Gateway SQLite 重新汇总的机器可读指标、限制、配置、catch-up 分段 |
| `summary.json` | 同上（精简摘要） |
| `gateway_events.csv` | 72,000 条事件级原始记录（通知号、店铺、状态、attempts、时间、错误类） |
| `gateway.db` | 本次临时 Gateway SQLite 完整备份（SQLite backup API，120,537,088 字节） |
| `full_run_console.log` | 长跑编排器控制台时间线（含 sink_down/up、每 5 秒进度） |
| `main_loadtest.log` | 主压测脚本进度日志（被中止时未产生最终报告） |
| `gateway_restart_*.log` | 测试性重启 Gateway 的空日志（进程已停止） |

事件级 CSV/DB 可复核行数、状态和 attempts；`raw_metrics.json` 可复核 P50/P95/P99 和 catch-up 分段。

## 3. 已完成的关键证据

### 3.1 流量与故障窗口

- 计划：72,000 主事件（3,600s × 20/s），100 店铺，重复率 2%、乱序率 5%、部分订单率 5%。
- 数据库实际：**72,000 行、100 个店铺、duplicate_count 合计 1,807**。
- 租户离线：控制台在长跑 elapsed≈1800s 置 `sink_down=True`，elapsed≈3600s 恢复；离线 30 分钟期间新流量继续写入 Gateway。
- 注入故障：relay 429 20 次、HTTP 回执丢失 20 次、worker/Redis 60 秒不可用模拟；这些均只作用于本地 sink。
- 数据库最终状态：delivered 47,660；pending 24,340；dead 0；unroutable 0；attempts 分布 1:63,841、2:891、3:298、4:50、0:6,920。

### 3.2 基线 relay 时延

`created_at→delivered_at`（Gateway 持久化到租户 relay 回执，不是订单写入）：

| 集合 | 数量 | P50 | P95 | P99 | Max |
| --- | ---: | ---: | ---: | ---: | ---: |
| 正常基线（离线前创建） | 35,981 | 247.10ms | 8,035.01ms | 141,970.05ms | 225,422.33ms |
| 离线期间创建且已投递 | 11,679 | 1,631,534.52ms | 1,833,500.17ms | 1,944,414.87ms | 2,031,939.99ms |
| 全部已投递 | 47,660 | 537.35ms | 1,789,095.69ms | 1,837,925.03ms | 2,031,939.99ms |

基线 P95 约 8s，低于 120s；但 P99 受 429/回执丢失/worker 短故障影响较大，且不是订单写入时延。

### 3.3 逐店铺分布

- 每店事件数：min **419**、max **9,034**、avg **720**。
- 热店/非热店均至少有流量；未做完整的按店 FIFO 顺序与饥饿专项分析。

## 4. 追平失败与测试环境干预

初始 Gateway 配置（local test env）：`global_concurrency=16`、`tenant_concurrency=8`、`store_concurrency=1`、`retry_base=15s`、`retry_max=300s`。

- 从离线恢复到 lead 停止期间，24,340 个事件仍 pending。
- **默认配置段**：09:56:36Z→10:15:56Z（约 1,160s）delivered 约 +4,561，约 **3.9 events/s**；按此速度 24k+ 积压无法在 60min 内追平。
- 为判断是否为并发配置瓶颈，**仅重启本地测试 Gateway**（未改核心代码、未接触生产）：
  - `global=128`、`tenant=64`、`store=1`、lease=120s，其余不变；
  - 10:15:56Z→10:19:21Z（约 205s）delivered +7,118，约 **34.7 events/s**；
  - 但 Lead 要求停止时仍有 24,340 pending，**没有完成 60 分钟追平条件**。
- 汇总 catch-up 平均吞吐（从恢复 09:56:36Z 至最后 delivered 10:19:21Z）约 **8.55/s**，低于 30/s；不能宣称追平通过。

> 该次 Gateway 重启是诊断/续跑措施，不是生产操作，也不是对核心代码的修改；它使 catch-up 指标混入了两段不同并发配置，报告已分段列出。

## 5. ACK 数据缺口

长跑主脚本被停止时仍在 `wait_for_drain`，未写最终 `webhook-loadtest-*.json`，因此：

- **全量 100 店/20/s/60min 的 ACK P50/P95/P99 不可得**；
- 不再补跑长跑（用户要求立即停止）；
- 可参考的独立小规模 smoke（非本次全量、不得当作全量 ACK）：`docs/webhook-loadtest-batch5.md` 记录 100 店/20/s/10s 的 ACK P99 **205.59ms**；更早的短 smoke 也均 P99 <1s。但本报告不将该值冒充 60 分钟全量 P99。

## 6. 未完成/未达项

1. **未完成最终 drain**：24,340 pending，不能证明逐事件完成/无遗漏；Gateway 侧无 dead/unroutable，但业务完成未验证。
2. **ACK 全量缺失**：主脚本未产出最终报告，P99≤1s 未证实。
3. **订单写入/库存未验证**：本地只有 Gateway→sink relay；未连接测试租户 MySQL，无法计算订单 `orders.created_at` 端到端或重复库存。
4. **重复库存未证明为 0**：仅有 gateway duplicate_count=1,807，不能推导库存。
5. **异常回执/429/worker 故障**只做了本地 sink 模拟；未验证真实 TikTok API 429/部分订单返回。
6. **Redis/Worker** 以 sink 不可用模拟，未停止真实隔离租户的 Redis/Worker；需独立测试租户环境补验。
7. **追平**未达 30/s/60min；默认配置 3.9/s，测试性高并发短时 34.7/s 但未完成全量。

## 7. 复现命令

```powershell
cd C:\Users\user\Desktop\ERP
$env:PYTHONIOENCODING='utf-8'
python scripts/webhook_loadtest_task47.py `
  --shops 100 --rate 20 --duration 3600 `
  --outage-start 1800 --outage-duration 1800 `
  --fault-429-at 300 --worker-fault-at 600 --receipt-fault-at 900 `
  --drain-timeout 5400 `
  --report-dir scripts/reports/task47
```

- 全程只启动 127.0.0.1 的临时 Gateway（SQLite 临时目录）和本地 sink；不连接生产。
- 该命令默认完整长跑约 1h+；本次因 Lead 要求提前中止，未产出最终 `loadtest` JSON。
- 如需完整复现，必须预留至少 2 小时并确认本地机器不被回收；Stop 前不要杀掉主压测进程。

## 8. 建议下一步

1. 在独立测试租户库（可快照恢复）重跑，并保留最终 `webhook-loadtest-*.json` 以获得 ACK/订单写入。
2. 对 Gateway 默认 dispatch 并发与 catch-up 策略做专项压测；本次显示默认 16/8 下 catch-up 仅约 4/s，而本地 128/64 短时可 >30/s。
3. 增加 tenant 级/店铺级积压曲线和 `last_attempt_at` 可视化，定位 pending 不降的根因。
4. 用真实隔离 Redis/Worker 和 TikTok 测试 double 补故障项；不要把本地 sink 模拟当真实结论。
