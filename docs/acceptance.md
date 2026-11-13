# 当前仓库状态（批次 4，2026-11-13，HEAD 5c9dd53）

> 批次 4 为“第二独立租户接入模板与迁移白名单”：只新增 `deploy/scripts/tenant-onboarding/`、
> `backend/scripts/tenant_migration_whitelist.py`、`docs/batch4-tenant-onboarding.md`，**未连接/修改任何生产**。
> 批次 3 及以前记录保留为历史。

| 检查 | 当前结果 |
| --- | --- |
| 后端测试 | `cd backend; python -m pytest -q` → **591 passed**，exit 0（1 条上游 Starlette 弃用提示） |
| Agent 测试 | `cd agent; python -m pytest -q` → **22 passed**，exit 0 |
| 完整契约检查 | **38/38 通过**，0 失败 0 警告（32 个迁移文件，`center_0018`/`tenant_0014`） |
| 批次 4 交付 | 第二租户 install/config/migrate/provision 模板、TENANT_ID/INSTANCE_ID/Worker 绑定校验、Agent2/SSH 指纹/备份初始化清单、迁移白名单与停写对账（行数/指纹/关联）、双实例隔离与异常场景验收清单 |
| 离线校验 | 8 个 shell 模板 `bash -n` 通过；白名单/environment/对账 `self-test` 通过；`config.sh` 渲染+绑定校验通过且会拒绝 Worker 不一致/未登记租户；install/migrate/reconcile/verify/provision 全部 plan 模式输出正常，未改系统 |
| 真实执行 | **未执行**：第二租户开通/域名切换/OAuth 回调重登记/店铺凭据迁移、`copy_data.sh`/`migrate.sh --apply`、停写恢复、断网/Agent 重启/发布失败/恢复验证真机演练；需 Lead 审批维护窗口 |
| 剩余风险 | 模板尚未真机验证；恢复演练仍为事故后冻结（禁止 `restore_drill.sh`）；独立网关（task-35）与恢复演练修复（task-34）进行中，不在本模板链路内 |

补充说明：

- 详细接入手册、白名单规则、双实例隔离清单与异常场景见 `docs/batch4-tenant-onboarding.md`。
- 本批次不宣称第二租户接入完成；模板默认 plan，任何 `--apply` 必须在目标实例、维护窗口、Lead 审批后执行。
- 恢复演练结论仍为**阻断/未通过**，见下文批次 3 历史记录。

---

# 批次 3 验收状态（历史，2026-11-13，离线 3ae757e / 生产试点 3ae757e）

> 本节为批次 3 历史结论；批次 3 详细证据见 `docs/batch3-acceptance.md`；备份/恢复约束见 `docs/backup-restore.md` 顶部 STOP。

| 检查 | 当前结果 |
| --- | --- |
| 后端测试 | `cd backend; python -m pytest -q` → **591 passed**，exit 0（1 条上游 Starlette 弃用提示） |
| Agent 测试 | `cd agent; python -m pytest -q` → **22 passed**，exit 0 |
| 前端构建 | `npm run build` exit 0（验收用临时 `--outDir`，未修改受版本管理的 `frontend/dist`；是否更新 dist 由 Lead 决定） |
| 迁移图 | 32 个迁移文件：中心库 `center_0018 (head)`（18 个）、租户库 `tenant_0014 (head)`（14 个）；离线 SQL 0 外键、中文注释完整 |
| 业务表 | 中心库 **29** 张、租户库 **33** 张（均不计 `alembic_version`）；`backup_records` 四阶段状态字段已写入 `docs/data-model.md` |
| 完整契约检查 | **38/38 通过**，0 失败 0 警告（补齐 `center_0018` 迁移基线后） |
| 批次 3 交付 | 备份加密/异地存储与 `backup_records` 状态（`center_0018`）、监控/请求性能/告警/watchdog、前端监控总览与备份阶段/维护窗口展示 |
| 生产试点 | `3ae757e` 中心/租户已部署；job 43 `succeeded`/`checks passed`，tenant HTTP 200；监控/性能/备份 UI 上线 |
| ⛔ 恢复演练（事故） | **阻断/未通过，禁止写“通过”**：2026-11-13 约 15:53 CST 在 8.163.80.17 执行旧版 `restore_drill.sh` 清空主 `ventasync_center`（0 表）并停止主容器；用 `/var/lib/ventasync-backups/releases/20261113T075322Z/ventasync_center.sql.gz`（138866 字节，UTC 07:53:22，`--no-record` 无 backup_records ID）恢复后中心库 30 表；compose up 后 api healthy、nginx started、租户 HTTP 200；随后备份 `20261113T075459Z` exit 0。当前脚本不安全，禁止任何生产机再运行，待 backend-sales 修复独立卷/安全断言后重新验收 |
| 剩余风险 | 恢复演练修复与隔离主机重新验收；生产恢复需 Lead 审批且备份记录可追溯；监控/告警真实环境持续观察；第二租户/网关/维护状态等仍在后续批次 |

补充说明：

- **恢复演练结论固定为阻断/未通过**：事故恢复成功不等于恢复演练通过；`restore_status=verified` 不得写入。
- `docs/backup-restore.md` 已加 STOP 警告、撤回旧版生产执行命令与“独立卷不碰生产”的错误断言，改列修复要求。
- 批次 2 试点（`1da80c2`）与批次 1 试点（`2e34e6b`）记录见下节历史及各自验收文档。

---

# 批次 2 验收状态（历史，2026-11-13，离线 02846cf / 试点 1da80c2）

> 本节为批次 2 历史结论；批次 2 详细证据见 `docs/batch2-acceptance.md`。

| 检查 | 当前结果 |
| --- | --- |
| 后端测试 | `cd backend; python -m pytest -q` → **531 passed**，exit 0（1 条上游 Starlette 弃用提示） |
| Agent 测试 | `cd agent; python -m pytest -q` → **22 passed**，exit 0 |
| 批次 2 特性测试 | tenant 模式边界/任务实例绑定/SSH 安全三文件 → **82 passed** |
| 前端构建 | `npm run build` exit 0（验收用临时 `--outDir`，未修改受版本管理的 `frontend/dist`；是否更新 dist 由 Lead 决定） |
| 迁移图 | 31 个迁移文件：中心库 `center_0017 (head)`（17 个）、租户库 `tenant_0014 (head)`（14 个）；离线 SQL 0 外键、中文注释完整 |
| 业务表 | 中心库 **29** 张、租户库 **33** 张（均不计 `alembic_version`）；`tenant_instances` 部署/迁移状态与 `server_nodes` SSH 指纹列已写入 `docs/data-model.md` |
| 完整契约检查 | **38/38 通过**，0 失败 0 警告：补齐 `center_0017` 迁移基线后恢复全绿；表清单/中文注释/权限码/94 条文档路由全部通过 |
| 批次 2 交付 | 租户模式边界（center/tenant/gateway、固定 TENANT_ID/INSTANCE_ID）、任务固定实例/租户绑定、迁移租户调度隔离、升级前强制备份、SSH 主机指纹 TOFU/审计/会话复核、实例部署/迁移状态展示 |
| 批次 2 生产试点 | **通过**：中心 `1da80c2` + `center_0017`；release 自动登记 verified/协议2/center_0017/tenant_0014；8.163.80.17 升级 job 42 成功（checks passed、actual=1da80c2、db=tenant_0014）；tenant 模式 TENANT_ID=1/INSTANCE_ID=1 登录/runtime/healthz 200、`/platform/releases` 404；SSH 首指纹已记录并 verified（SHA256:T2cB1vQn5bvCsJ9BPqmui5IIb+7vR/n1tuseQFY8LYE） |
| 剩余风险 | tenant 模式第二租户/多实例配置待验证；SSH 首指纹需纳入运维核对、mismatch 真实演练待补；审计标签待核对；升级前备份步骤因旧 executor 未在 job 42 生效（待 Agent 升级后补验）；备份恢复演练、维护状态、监控、独立网关、第二租户未完成（详见 `docs/batch2-acceptance.md` 第 5 节） |

补充说明：

- 批次 2 已完成单台生产试点（8.163.80.17）；升级前备份阻断真实路径、备份恢复、维护状态、监控/网关、第二租户仍未验收，
  上述离线/单台试点结论不替代批次 2 全量完成。
- 批次 1 试点（8.163.80.17，`2e34e6b`）记录见下节及 `docs/batch1-acceptance.md`。

---

# 批次 1 验收状态（历史，2026-11-13，离线 6828c2d / 试点 2e34e6b）

> 本节对应当前仓库与第一台试点实测；下方历史章节保留各次发布时点的记录，其迁移数/表数/测试数**不代表当前仓库**。
> 批次 1 详细证据见 `docs/batch1-acceptance.md`，基线对照见 `docs/batch1-baseline.md`。

| 检查 | 当前结果 |
| --- | --- |
| 后端测试 | `cd backend; python -m pytest -q` → **449 passed**，exit 0（1 条上游 Starlette 弃用提示） |
| Agent 测试 | `cd agent; python -m pytest -q` → **22 passed**，exit 0 |
| 前端构建 | `npm run build` exit 0（验收用临时 `--outDir`，未修改受版本管理的 `frontend/dist`；是否更新 dist 由 Lead 决定） |
| 迁移图 | 30 个迁移文件：中心库 `center_0016 (head)`（16 个）、租户库 `tenant_0014 (head)`（14 个）；离线 SQL 可生成、0 外键、中文注释完整 |
| 业务表 | 中心库 **29** 张、租户库 **33** 张（均不计 `alembic_version`）；已与 `docs/data-model.md` 同步 |
| 完整契约检查 | **38/38 通过**，0 失败 0 警告：零外键、表清单/迁移图/中文注释/权限码/94 条文档路由全部通过 |
| 场景模拟 | 重复事件幂等、租约续租、unknown 不重试、升级检查失败停止变更、旧 Agent 不领取变更任务：均有自动化用例并通过（详见 `docs/batch1-acceptance.md` 第 3 节） |
| 第一台试点（8.163.80.17） | **通过**：中心 `2e34e6b` + `center_0016`；发布自动登记 `verified/2/center_0016/tenant_0014`；Agent2 双 unit active、protocol=2、2.0.0；升级 job 41 `checks_status=passed`，实例 `actual_version=2e34e6b`、`db_version=tenant_0014`、`upgrade_status=succeeded`，节点心跳回写 `2e34e6b` |
| 失败演练 | job 35 SQLite 只读、36 invalid_commit 拒绝、37/38 build/readiness 失败并修复、39 partial、40/41 passed（详见 `docs/batch1-acceptance.md` 第 2.4 节） |
| 剩余项 | 备份前置与恢复演练、维护状态、第二租户试点（第二批）；SSH 指纹/审计、监控、独立网关（第三批）；详见 `docs/batch1-acceptance.md` 第 5 节 |

补充说明：

- **追加型表例外（Lead 决策）**：`node_metric_samples`、`node_checks`、`operation_job_steps`、`backup_records` 不要求
  `created_at/updated_at`，`node_metric_aggregates` 不要求 `updated_at`；以 `sampled_at`/`checked_at`/`started_at`/`finished_at`/`bucket_start`
  等业务时间字段作为追加时间。其余表仍要求 `id`、`created_at`、`updated_at`。
- 第一台试点已覆盖 ERP 发布/升级链路；真实平台订单库存闭环、第二租户、备份恢复等未完成，不宣称批次 1 全量完成。
- 历史章节中“38/38 通过、22 个迁移、中心 18 张/租户 32 张”等数字是各自发布时点的记录，不是当前 HEAD 结论。

---

# 租户内成员编号验收（2026-11-12）

业务提交`7cbe4b8`已推送并部署，中心库升级至`center_0009`，三个租户库保持`tenant_0013`。

- 每个租户的成员编号从1独立递增，成员列表显示、复制`member_no`；开通、邀请及兼容接受邀请路径统一在租户行锁下分配，使用持久高水位避免重复或复用。
- 历史成员按每租户`created_at,id`回填。停用、重邀、激活、开通重试和负责人转移均保号；内部全局成员及账号ID保留，原角色、商品人员与任务关联不改写。
- **79项相关回归、38项完整契约及npm run build通过**；22个迁移，中心18张/租户32张业务表，中文注释完整、零外键。
- 生产三租户编号分别为`[1,2]`、`[1]`、`[1]`，下一编号分别为3、2、2。可用租户身份的成员列表、bootstrap、租户信息和商品人员选项接口200，返回租户内编号且保持原内部ID。
- 迁移前后关键成员、角色绑定、数据范围、商品及库存等字段指纹和行数一致；中心库、全部租户库及图片卷备份校验通过；API readyz、应用容器和公开前端产物检查通过。

生产证据见`/opt/backups/releases/erp-7cbe4b8/`及real-deployment.md §16。本地未启动前后端服务。

---

# OBS连接故障修复与真实云验证（2026-11-12）

业务提交`90f8ced`已推送并部署。已复现两个原因：保存的Region使用中文地域名称导致SDK创建时抛InvalidRegionError；改为区域编码后，旧OBS自动寻址发送路径格式请求返回403，桶域名格式返回200。

- 已通过原配置服务更正Region为`cn-south-1`、寻址方式为virtual，保留原密钥并记录配置审计与版本。
- OBS auto现使用virtual，保持S3 V4及TLS验证；保存时拒绝中文区域名称、OBS path及当前HTTPS适配不支持的带点桶名。页面明确提示区域编码与留空自动识别。
- 连接和上传失败使用固定中文提示与白名单provider_code，不返回SDK异常正文、密钥或签名。
- **50项相关后端测试、npm run build通过**；线上新代码的auto HeadBucket以及平台页面同一路径的POST连接测试均200，公开页面及资源哈希检查通过。
- 真实小图上传成功，常规商品图片目录的匿名GET为**403 AccessDenied**；桶ACL虽包含匿名READ，仍不代表对象可读。临时文件均已删除，未修改桶ACL、桶策略或其他对象权限。图片外链访问仍需配置对象读权限或CDN回源授权。

本次无数据库结构迁移。生产证据与中心库备份见`/opt/backups/releases/erp-90f8ced/`及real-deployment.md §15。

---

# 基础设置与商品设置验收（2026-11-12）

本轮代码已提交为`3658871`并于2026-11-12部署到`https://erp.alienyan.store/`。中心库已升级至`center_0007`，两个租户库均为`tenant_0013`；线上验证见real-deployment.md §14。本地未启动前后端服务。

- 租户菜单新增“基础设置 → 商品设置”，页面路径`/settings/products`；供应商、品牌、分类、特性标签、包材集中管理，原SPU表单只保留选择器。
- 五类资料支持搜索、分页、新增、编辑和启停；供应商编号及名称必填，联系人、联系方式、备注选填且可清空。
- 维护操作要求全租户商品编辑权限，由后端返回能力控制页面按钮；接口校验租户归属、重复编号/名称、分类父级与循环，成功变更与审计同事务提交。
- 停用不删除历史资料；原SPU可以保留已停用供应商及字典值并修改其他字段，新选择已停用资料会被拦截。品牌/分类/标签/包材已经被商品引用时，改名返回409。
- 供应商增量迁移为`tenant_0013`（父版本`tenant_0012`），新增三个可空字段，中文字段注释，无外键。部署时先完成迁移再切换代码。

| 检查 | 结果 |
| --- | --- |
| 相关后端回归 | **18 passed**：9项商品服务、4项基础资料HTTP、5项既有商品HTTP |
| 完整契约检查 | **38/38通过**，20个迁移，中心17张/租户32张业务表，中文注释完整、零外键 |
| 前端生产构建 | **npm run build通过**，包含TypeScript检查，dist已更新 |
| 代码差异检查 | git diff --check通过 |
| 生产验证 | 三库关键业务指纹及行数一致，中文注释完整、零外键；公开页面与静态文件哈希、商品及系统配置鉴权GET检查通过 |

---

# 系统配置与云对象存储验收（2026-11-12）

本轮代码已随商品设置在`3658871`发布，中心库`center_0007`、租户库`tenant_0013`。下方SPU发布记录属于上一版本。初次发布使用SDK stub/模拟客户端验证；随后已配置OBS并完成连接、真实上传及删除验证，匿名图片读取仍返回403，详见本文件顶部OBS修复记录。

## 完成功能

- 平台管理新增“系统配置”，仅平台管理员会话可管理 OSS、COS、OBS、AWS S3 和 S3 兼容服务账号。
- 支持多个账号、默认账号切换、启停非默认账号、存储桶连接测试；编辑使用版本校验。密钥加密保存，页面及审计不回显凭据，留空保留同厂商旧凭据。
- 新图片通过默认账号上传，数据库文件记录保存厂商、配置ID、对象键和长期HTTPS URL；商品图片和SKU图片保存该URL。切换默认账号不会改写已有文件URL。
- 未配置默认账号或云上传失败时明确失败，不退回本地写盘；新图片须由本租户上传登记后才能关联商品。现有本地图片仍通过鉴权接口读取。
- 厂商协议采用统一Boto3适配，OSS/COS使用V2，OBS/S3使用V4；显式凭据、超时和有限重试。图片响应策略允许HTTPS云域名，脚本及接口保持同源。

## 检查结果

| 检查 | 结果 |
| --- | --- |
| 后端完整回归 | **385 passed**，包括权限/凭据脱敏/版本/默认切换/上传URL入库/历史URL和本地图片兼容 |
| 完整契约检查 | **38/38通过**，19个迁移，中心17张业务表、租户32张业务表 |
| 前端生产构建 | **npm run build通过**，dist已更新 |
| 数据库规范 | 模型与最终迁移SQL零外键、中文注释完整 |
| 真云边界 | 初次发布仅stub；现已完成OBS HeadBucket、真实图片上传及删除验证，匿名读取仍403。页面连接测试只执行HeadBucket，不代表图片公开访问已经通过 |

## 发布与配置

1. 已备份中心库、两个租户库及旧图片卷，升级至`center_0007`、`tenant_0013`后更新API/Worker/scheduler/nginx；新增依赖`boto3==1.44.1`。本次服务器镜像源尚未同步该版本，官方源下载较慢，使用本地下载并校验的Linux依赖包完成离线构建，requirements保持一致。
2. 平台管理员进入 `/platform/system-settings`，填写厂商、Endpoint、Bucket、区域、访问密钥、长期访问域名/CDN、目录前缀和寻址方式，保存后检查连接并设为默认。
3. 使用公开读桶或可长期读取对象的CDN域名；不保存会过期的签名URL，也不由程序自动改变桶/对象ACL。首次启用后需实际上传并打开返回URL验证。
4. 当前版本新上传必须配置默认账号；未配置时会返回明确提示。原有图片卷应继续保留，本轮不自动迁移历史本地图片。
5. 对象存储中的文件由云厂商管理备份/生命周期；原有脚本继续备份历史本地卷，数据库备份保存文件URL和加密的账号配置，保留现有ENCRYPTION_KEY。

---

# SPU—SKU 商品中心验收（2026-11-12）

本轮已完成本地实现并于 2026-11-12 发布到 https://erp.alienyan.store/。两个租户库均升级至 tenant_0011，关键业务字段指纹、真实库规范、商品接口、图片卷和备份检查通过，发布证据见 real-deployment.md §13。

## 完成内容

- 沿用现有商品及 SKU 主键，补齐 SPU 基本资料、供应商简档、人员选择、品牌分类标签包材字典、尺寸和销售类型。
- 商品列表按 SPU 展示，独立详情包含基本信息、销售信息、操作日志；支持图片上传、排序、SKU 批量修改预览及版本冲突检测。
- SKU 多别名与主编号共享租户唯一标识空间，编码更改保留旧编号；TikTok 商品和订单支持按别名识别，历史店铺映射保留。
- SPU 的三类成本和重量逐字段汇总；销售状态按全部一致、部分清仓、部分停售、在售的顺序计算。
- SPU/SKU 成功修改与字段差异日志同事务保存，显示旧值、新值、秒级时间及操作人；商品日志和全局审计统一过滤无权限成本。
- 订单新增原生 SKU 标识兼容层：商家编码补全或变化时原位识别历史明细，歧义阻止整笔写入，避免重复扣减库存。
- 商品图片采用同源鉴权接口和持久化卷；全租户备份脚本在卷存在时同时归档图片并生成 SHA-256。

## 本轮检查结果

| 检查 | 结果 |
| --- | --- |
| 后端单元及契约回归 | **346 passed**，包含进程内 HTTP、临时 SQLite、模拟平台响应；未启动应用服务 |
| 完整契约检查 | **38/38 通过**，17 个迁移，15 张中心业务表、32 张租户业务表 |
| 前端生产构建 | **npm run build 通过**，受管理的 dist 已更新 |
| 数据库规范 | 模型、全部迁移和最终离线 SQL 零外键，中文注释完整 |
| 图片部署配置 | Compose 持久化卷关联检查通过；备份脚本 bash -n 通过 |
| 差异检查 | git diff --check 通过 |

后端存在一条上游 TestClient/httpx 弃用提示，不影响检查结果。

## 升级及验收边界

1. 发布前备份中心库、全部租户库和已有图片。执行在线 Alembic 增量迁移至 `tenant_0011`，再更新 API/Worker/scheduler/nginx；中心库末端仍为 `center_0006`。
2. 新迁移会预检主编号标准化冲突，登记统一标识，补齐空 SKU 的历史当前成本、历史 SKU 图片及品牌分类字典，并给订单明细增加原生 SKU 标识。出现冲突需处理数据后再升级，不自动改号。
3. 离线 SQL 用于结构审阅。Python `casefold()` 的非 ASCII 历史编号回填应通过在线迁移完成，不直接使用离线 SQL 替代。
4. 图片目录为 `/app/var/catalog-media`，Compose 命名卷 `ventasync_catalog_media_data`；`backup.sh --all-tenants` 在该卷挂载时备份图片。恢复图片时需与对应数据库备份配套恢复并保持应用 UID/GID 10001 的读取权限。
5. 本轮已验证真实 MySQL 增量迁移、两个租户库的结构和关键业务字段，以及图片卷读写和备份；高并发行锁、图片卷恢复演练及 TikTok 真店订单仍待后续验收。
6. 历史外部图片地址受现有 CSP 限制；本轮新上传图片为同源地址。平台图片代理、在线调价、促销和刊登仍是下一阶段工作。

## 本轮生产发布结果

- 业务提交 `4bfe4f7`，迁移审计必填字段修复 `ebd211f`，均已推送远程并发布。
- 中心库 `center_0006`，两个租户库均为 `tenant_0011`；业务表分别 15、32、32 张，外键均为 0，中文注释完整。
- 迁移首次在审计 INSERT 缺少历史表 `ip/request_id` 必填值处失败。已补齐字段并新增一项真实 SQL 回归通过；第一个库核对全部表/列及此次新增索引后，只重放回滚的数据回填并在同一事务确认版本；第二个库通过修复后的正常迁移完整升级。
- 发布前后关键商品、SKU、订单、明细、库存和映射字段的 SHA-256 指纹一致；主编号与 SKU 的登记核验通过。预期的成本/图片回填、新资料字段和迁移日志不纳入不变字段指纹。
- 活跃且具共享商品读取权限的租户身份通过公网访问商品列表、详情、SKU 搜索、选项和操作日志，均返回 200；临时验证会话已清理，没有新建或修改商品业务数据。
- 图片卷以 UID 10001 可读写，临时文件已清理；发布后的图片归档和两个租户库备份均通过 SHA-256 校验。
- API/Worker/nginx/MySQL/Redis 均健康，scheduler 运行；公网首页与登录页 200，主 JS/CSS 与本次构建哈希一致。
- 服务器备份、迁移恢复脚本、结构前后快照、业务字段指纹和接口验证结果保存在 `/opt/backups/releases/erp-4bfe4f7/`，不进入 Git。

---

# 上轮安全修复与发布记录（2026-11-12）

本轮先完成本地代码、内存数据库、离线迁移及构建验证，随后按用户要求于 2026-11-12 发布到 `https://erp.alienyan.store/`。中心库与两个租户库已完成增量升级，生产库结构、关键表行数和站点检查通过，证据参见 `real-deployment.md` §12。真实电商店铺授权、订单及库存推送闭环仍未验收。

## 关键回归范围

| 范围 | 必须验证的行为 | 证据位置 |
| --- | --- | --- |
| 邀请身份 | 已有账号必须以匹配身份登录；错误账号、未登录、停用账号不能通过邀请取得会话 | identity 回归 |
| 角色范围 | 局部业务权限不能经编辑已有全局角色扩展到全租户；所有者角色不能被清空 | identity 回归 |
| 任务 API | 只读成员不能修改调度；列表、详情、状态、重试遵守店铺范围；UUID 无登记不可查询 | test_task_security.py |
| 同步开关 | 创建、调度、Outbox、Worker 和平台调用前检查总开关；权限版本递增 | task/channel 回归 |
| 执行身份 | 手动任务保留发起人，不与其他人或自动化 pending 任务合并，不退化为所有者 | task/sales/channel 回归 |
| 多渠道订单 | Worker 根据店铺渠道解析 TikTok/Shopee，检查点按租户/渠道/环境/店铺隔离 | sales worker 回归 |
| 分页与重试 | 未处理完窗口不得推进水位；重试和重复响应不重复扣减库存 | sales worker 回归 |
| 授权入口 | 长期入口可撤销；短期 state 有效 600 秒且只消费一次；拒绝旧永久 state；多店选择与重新授权身份校验 | OAuth 回归 |
| 数据库 | 当前模型、实际全部迁移及最终 SQL 零外键，CREATE/ADD/MODIFY 后中文注释完整 | verify_contracts.py |

## 本地检查

本轮集成执行结果：

| 检查 | 结果 |
| --- | --- |
| 后端单元与契约回归 | **319 passed**，包含账号邀请、角色范围、任务授权、断点、额度和授权恢复回归 |
| 完整契约校验 | **38/38 通过**；实际发现 16 个迁移文件 |
| 前端生产构建 | **npm run build 通过**（vue-tsc + Vite），已更新受管理的 dist |
| 数据库约束 | 15 张中心业务表、29 张租户业务表；模型与完整离线 SQL 零外键，最终中文注释检查通过 |
| 工作区差异 | git diff --check 通过 |

测试有一条上游 TestClient/httpx 弃用提示，不影响本轮结果。MySQL 行锁、Redis Worker 故障恢复及真实平台结果仍按下面的独立环境清单验收。

```powershell
cd backend
python -m pytest tests/unit tests/contract -q
python scripts/verify_contracts.py
python -m alembic -c alembic/center.ini heads
python -m alembic -c alembic/tenant.ini heads
cd ../frontend
npm run build
```

当前迁移末端：中心库 `center_0006`，租户库 `tenant_0010`。业务表数量为中心库 15、租户库 29，不计 Alembic 版本表。

## 本轮发布注意事项

- 先发布中心库 `center_0006` 和租户库 `tenant_0008` 至 `tenant_0010` 增量迁移，再启用新代码；API 不自动迁移。
- 本轮会话采用身份版本 2，旧会话在升级后失效，所有用户需要重新登录。
- 旧永久签名 OAuth state 不再接受；需要从店铺页面重新生成可撤销授权链接。已保存的平台店铺凭据不因此清除。
- 主商品是租户共享资料：查询、创建、修改与归档需要相应操作的全部范围；仅有店铺范围的角色管理店铺刊登，不修改共享资料。
- 成员（已邀请或有效）、店铺、商品和 SKU 创建遵守当前套餐额度；归档、断开记录仍占存储额度，套餐降级不删除既有业务。
- TikTok 增量查询改按更新时间窗口，正式试点需验证平台参数及权限；不把模拟响应视作真实 API 验收。

## 独立环境待验收

1. 在隔离 MySQL/Redis 环境执行全部增量迁移，检查零外键及最终字段注释，确认可从旧版本升级。
2. 两租户相同 ID、SKU 下，API、缓存、文件与队列不串租户。
3. 同 SKU 并发订单、取消恢复、重复回调不丢库存变化。
4. 关闭推送后已有队列任务不再请求平台；Worker 中断后任务可恢复且不回写旧库存。
5. TikTok 真实授权、Token 刷新、商品映射、拉单、库存核对与受控推送。
6. TikTok 通过后执行 Shopee 同等闭环。
7. 单租户备份恢复后先对账再开启总开关，确认其他租户不受影响。

每个真实验收记录环境、代码版本、时间、输入规模、预期、实际和脱敏证据。模拟 HTTP 响应和 SQLite 回归不能替代 MySQL 行锁及真实平台验收。
