# 部署专项：Redis 队列内存与 tiktok_refresh 专用 Worker（task-44）

> 面向 2C2G 单机租户：百店容量前的运行时资源与 Worker 队列隔离。
> **本文与配套 compose/env 均为待上线草案；本轮未重启、未修改任何生产容器/配置。**

## 1. 审计结论（变更前）

| 项目 | 变更前 | 问题 |
| --- | --- | --- |
| 队列 Redis maxmemory | 生产 `.env` 为 `192MiB`（仓库示例 512mb） | 容器 `mem_limit=160m`，`maxmemory > mem_limit`；Redis 进程开销/AOF rewrite 前就可能被 cgroup OOM 杀死 |
| 队列 Redis 策略 | `noeviction` + AOF `everysec` | 正确，保持；但内存不足时写入直接失败，不会静默丢任务 |
| 普通 Worker | `-Q default,outbox,scheduler,channel,sales --concurrency=${CELERY_CONCURRENCY:-4}`，生产 `.env=1` | 只有 1 个执行位且与 5 个队列共享；TikTok 刷新任务排队会阻塞订单/库存等普通任务 |
| 专用刷新 Worker | 无 | 无 `tiktok_refresh` 队列消费位 |
| 内存预算 | mysql 700m + api 420m + worker 420m + scheduler 160m + redis-cache 96m + redis-queue 160m + nginx 64m ≈ 2.02GiB | 2GiB 机器加 Caddy/OS 已超卖，新增 Worker 无空间 |

## 2. 目标配置（本次提交）

### 2.1 队列 Redis

```text
容器上限（mem_limit）      160m
Redis maxmemory            96MiB = 100663296 bytes
maxmemory-policy           noeviction（保持）
AOF                        appendonly yes, appendfsync everysec（保持）
save                       ""（不启用 RDB 周期快照，保持）
余量                       约 64MiB 给 Redis 进程、客户端缓冲、AOF rewrite fork/COW
```

`deploy/docker-compose.yml` 的默认值已改为 `${QUEUE_REDIS_MAXMEMORY:-96mb}`；
**生产实际值由服务器 `deploy/.env` 覆盖**，上线窗口必须把生产 `.env` 同步改为
`QUEUE_REDIS_MAXMEMORY=96mb`，否则 compose 默认值不会生效。

### 2.2 Worker 执行位

| Worker | 队列 | concurrency | 启动方式 |
| --- | --- | --- | --- |
| `worker`（普通业务） | `default,outbox,scheduler,channel,sales` | `1`（`CELERY_CONCURRENCY=1`） | 默认启动 |
| `tiktok-refresh-worker` | 仅 `tiktok_refresh` | `1`（`TIKTOK_REFRESH_CONCURRENCY=1`） | `--profile tiktok-refresh`，默认不启动 |

专用 Worker 使用独立 `--hostname tiktok-refresh@%h`，`worker_prefetch_multiplier=1`
保持 Celery 默认配置；不与普通业务队列共享执行位。

**task-40 依赖（重要）**：`tiktok_refresh` 队列路由/任务（generation + lease +
`process_tiktok_webhook_events` 等）由 task-40 实现。当前若队列代码尚未上线：

* 保持 `tiktok-refresh-worker` profile 关闭（默认状态即可）；
* 不要向 `tiktok_refresh` 投递任务，否则普通 worker 不消费该队列（当前 `-Q` 不含），任务会积压；
* task-40 镜像上线并确认路由/迁移完成后，再在发布窗口开启 profile。

### 2.3 2C2G 资源建议

内存上限（`docker-compose.server.yml` + 生产 `.env`）：

| 服务 | 建议上限 | 说明 |
| --- | ---: | --- |
| mysql | 600m | `innodb_buffer_pool_size=128M`、`performance_schema=OFF` |
| api | 360m | uvicorn 单进程 + 连接池 |
| worker（普通） | 320m | concurrency=1，Python/Celery 基础内存 |
| tiktok-refresh-worker | 200m | concurrency=1，独立队列 |
| scheduler | 120m | 单进程扫描 |
| redis-cache | 96m | `maxmemory=64mb`，容器需留进程余量 |
| redis-queue | 160m | `maxmemory=96MiB`，留 AOF rewrite 余量 |
| nginx | 64m | 静态 + 反代 |
| **合计** | **≈1.92GiB** | 另需 **2GiB swap**；Caddy/OS 不计入 compose |

CPU（2 vCPU，建议以共享权重而非硬 cap，避免互相节流）：
mysql 0.7、api 0.5、worker 0.35、tiktok-refresh 0.25、scheduler 0.05、
redis-cache/queue 各 0.05、nginx 0.05（合计约 2.0 vCPU）。

MySQL 连接数（2C2G、≤2 租户）：建议 `MYSQL_MAX_CONNECTIONS=150`，并监控
`Threads_connected` / `Threads_running`。理论上限≈4 个后端进程 ×（中心池 10+10 +
租户数×(TENANT_POOL_SIZE 5 + TENANT_MAX_OVERFLOW 5)）。若连接持续接近上限，优先
下调 `TENANT_POOL_SIZE/TENANT_MAX_OVERFLOW`（例如 3/3）而不是盲目加连接。

## 3. 上线窗口与步骤（不重启当前生产）

当前只提交编排/示例/文档，未执行任何上线命令。建议在低峰维护窗口按以下顺序：

### 3.1 窗口前（只读）

```bash
cd /opt/ventasync
git log --oneline -1
# 只读巡检：compose 校验 + Redis 使用率/队列积压/Worker 状态
bash deploy/scripts/check_deploy44_runtime.sh
# 记录：used_memory、maxmemory、AOF 状态、队列长度、容器 OOM 记录
docker inspect --format '{{.Name}} {{.State.OOMKilled}} {{.HostConfig.Memory}}' \
  "$(docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.server.yml \
      --env-file deploy/.env ps -q redis-queue)"
```

**停止条件**：若 `used_memory` 已超过 96MiB 或 `tiktok_refresh` 有积压，先扩容/清理并
增加 swap，再继续；不要直接降 maxmemory 后重启 Redis。

### 3.2 窗口内

```bash
# ① 更新服务器 deploy/.env（示例值；保留原有密码/域名）
#   QUEUE_REDIS_MAXMEMORY=96mb
#   CACHE_REDIS_MAXMEMORY=64mb
#   MYSQL_MAX_CONNECTIONS=150
#   CELERY_CONCURRENCY=1
#   TIKTOK_REFRESH_QUEUE=tiktok_refresh
#   TIKTOK_REFRESH_CONCURRENCY=1
#   TIKTOK_REFRESH_WORKER_MEM_LIMIT=200m
#   （低配覆盖的内存项见 .env.example）

# ② 只校验，不启动
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.server.yml \
  --env-file deploy/.env config -q

# ③ 重建 redis-queue 使 maxmemory 生效（AOF 持久化，短暂 broker 重启；
#    先确认没有大量在途任务，worker 会自动重连）
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.server.yml \
  --env-file deploy/.env up -d --no-deps --force-recreate redis-queue

# ④ 重建普通 worker 使 concurrency=1 生效（单副本短暂中断）
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.server.yml \
  --env-file deploy/.env up -d --no-deps --force-recreate worker

# ⑤ task-40 队列路由/镜像确认后再启用专用 Worker
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.server.yml \
  --env-file deploy/.env --profile tiktok-refresh up -d --no-deps tiktok-refresh-worker
```

也可以不重建 Redis，用 `CONFIG SET maxmemory 100663296` 在线调整；但这同样要求
`used_memory < 96MiB`，且需要把 `.env`/compose 同步改好，否则下次重建会回退。

### 3.3 窗口后验证

```bash
bash deploy/scripts/check_deploy44_runtime.sh
docker compose ... exec -T redis-queue redis-cli INFO memory | grep -E 'used_memory_human|maxmemory_human'
docker compose ... exec -T redis-queue redis-cli CONFIG GET maxmemory-policy appendfsync
docker compose ... exec -T worker celery -A app.workers.celery_app inspect ping
docker compose ... --profile tiktok-refresh exec -T tiktok-refresh-worker \
  celery -A app.workers.celery_app inspect ping
# 任务/订单链路冒烟：1 个测试租户手动同步 + 查看 tiktok_refresh 队列消费
```

### 3.4 回滚

```bash
# 回滚普通 worker 并发（原值按生产 .env）
#   CELERY_CONCURRENCY=1（保持 1 也兼容旧行为）
# 回滚队列 maxmemory 到变更前值（例如 192mb），然后重建 redis-queue
# 停止专用 Worker（不影响已有任务，任务留在 Redis 等下次消费）
docker compose ... --profile tiktok-refresh stop tiktok-refresh-worker
docker compose ... --profile tiktok-refresh rm -f tiktok-refresh-worker
```

回滚不删除 Redis 命名卷；AOF 保留待执行任务。若从 96MiB 回滚到更大值，可直接
`CONFIG SET` 或重建，风险低于降内存。

## 4. 校验命令（仓库本地，无需生产）

```bash
# 基础编排
docker compose -f deploy/docker-compose.yml --env-file deploy/.env.example config -q

# 2C2G 覆盖 + 专用 Worker profile
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.server.yml \
  --env-file deploy/.env.example --profile tiktok-refresh config -q

# 查看 tiktok-refresh-worker 最终 command（应为 -Q tiktok_refresh --concurrency 1）
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.server.yml \
  --env-file deploy/.env.example --profile tiktok-refresh config \
  | grep -A 30 'tiktok-refresh-worker:'
```

## 5. 风险清单

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 生产 `.env` 未同步 96MiB | compose 默认值被旧 192MiB 覆盖，仍可能 OOM | 窗口内显式修改并 `docker compose config` 核对 maxmemory |
| 当前 used_memory >96MiB | noeviction 下新任务写入直接失败 | 窗口前巡检；先扩容/清积压/加 swap，禁止盲目重启 |
| AOF rewrite fork/COW | 容器 160m 被击穿 OOM | maxmemory 96 + 160m 上限留 64MiB；监控 `aof_rewrite_in_progress`/OOMKilled |
| 重建 redis-queue/worker | 短暂 broker/worker 中断，API 入队失败 | 低峰窗口；AOF 持久化；发布前备份；worker 自动重连 |
| task-40 未上线即启用 profile/投递 | `tiktok_refresh` 积压无人消费 | profile 默认关闭；task-40 路由+迁移验证后再开；先小流量 |
| 2G 内存预算过紧 | MySQL/API/Worker 被 OOM killer 杀 | 按表设置 mem_limit、2GiB swap、监控 container OOM/dmesg |
| MySQL 连接数不足 | 百店并发下 `Too many connections` | `MYSQL_MAX_CONNECTIONS=150` + 监控 Threads_connected；必要时下调租户池 |

## 6. 与 task-40 的依赖

* 本任务只做部署编排/env/脚本/文档，**不改变业务投递逻辑**；
* `tiktok_refresh` 队列名在 compose/env 中已预留；实际任务路由
  （`task_routes` / worker 消费）必须由 task-40 提供；
* task-40 未合并前，`tiktok-refresh-worker` 保持 profile 关闭，普通 worker `-Q` 不含
  `tiktok_refresh`，不会误消费也不会抢占普通队列；
* task-40 合并并部署新镜像后，按 3.2 第⑤步启用专用 Worker，再用
  `deploy/scripts/check_deploy44_runtime.sh` 核对队列长度与消费。
