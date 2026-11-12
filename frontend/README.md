# ERP 前端（多租户 SaaS 后台）

Vue 3.5 + TypeScript + Vite + Vue Router 4 + Pinia 3 + Element Plus 2 首版后台工程。
只负责前端工程与页面，数据全部按 `docs/api-contract.md` 契约调用 `/api/v1`。

## 快速开始

```bash
npm install
npm run dev      # 开发：Vite 默认 5173，/api 代理到 http://127.0.0.1:8000
npm run build    # 硬性门槛：vue-tsc 类型检查 + vite 生产构建
npm run preview  # 预览构建产物
npm run typecheck
```

环境变量（见 `.env.example`，不要写死后端地址）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `/api/v1` | 前端请求前缀，生产由 Nginx 反代 |
| `VITE_API_PROXY_TARGET` | `http://127.0.0.1:8000` | 仅开发代理目标 |
| `VITE_DEV_PORT` | `5173` | 开发端口 |

## 目录结构

```text
src/
  api/        http.ts（Cookie/CSRF/错误/取消范围）+ auth、tenant、platform、catalog、inventory、channels、sales、tasks
  stores/     auth（当前用户与租户列表）、tenant（当前租户 + bootstrap 权限 + 世代）
  router/     routes（页面/权限元数据）、guard（登录/租户/权限守卫）、menu（按权限生成菜单）、home（首页解析）
  composables/ useTableQuery（列表分页/迟到响应保护）、useTenantTask（202 任务轮询）、useEnterSession
  directives/ v-permission（无权限移除元素）
  components/ StatusTag、PaginationBar、JsonBlock、IdCopy、ScopeBindingEditor
  layouts/    MainLayout（左侧权限菜单 + 顶部租户切换/用户菜单）
  views/      auth（登录/邀请激活/租户选择）、platform（租户/套餐/渠道应用/平台审计）、tenant（成员/角色/商品/库存/店铺/映射/订单/任务/审计）
  types/      按契约声明的请求响应类型（大整数 ID 一律 string）
  utils/      格式化、状态字典、任务轮询
  styles/     全局样式
```

## 页面与路由

| 路由 | 页面 | 权限/条件 |
| --- | --- | --- |
| `/login` | 登录 | 公开 |
| `/activate/:token` | 邀请激活 | 公开 |
| `/tenant-select` | 租户选择 | 登录 |
| `/forbidden` | 无权限 | 登录 |
| `/products` | 商品（列表/新建/编辑/归档/详情 + SKU 子表） | `catalog.product.read` |
| `/inventory` | 库存（余额/调整/流水/地点） | `inventory.read` |
| `/stores` | 店铺连接（Shopee 授权/断开/凭据状态） | `store.read` |
| `/listings` | 平台商品映射（自动匹配状态/手动映射/同步） | `channel.listing.read` |
| `/orders` | 订单列表（筛选/手动同步） | `order.read` |
| `/orders/:orderId` | 订单详情（明细/状态历史/库存影响/原始数据） | `order.read` |
| `/sync-runs` | 任务记录（sync-runs/重试/调度开关） | `task.read` |
| `/members` | 成员（邀请/停用/角色与数据范围） | `member.read` |
| `/roles` | 角色（列表/新建/编辑权限勾选/删除） | `role.manage` |
| `/audit-logs` | 租户审计 | `audit.read` |
| `/platform/tenants` | 平台租户管理（创建/重试开通/暂停/启用/详情/负责人邀请链接生成与复制） | 平台管理员 |
| `/platform/plans` | 套餐与功能额度 | 平台管理员 |
| `/platform/channel-apps` | Shopee 应用凭据（密钥不回显） | 平台管理员 |
| `/platform/audit-logs` | 平台审计 | 平台管理员 |
| `/` | 按有效权限跳转首个可访问页面 | 登录 + 租户 |

## 关键约定

* **认证**：同源 Cookie 会话（HttpOnly），前端不保存 token；`GET /me` 探测登录态。
* **CSRF**：写请求从可读 Cookie `vs_csrf` 取令牌放 `X-CSRF-Token`；缺失时先 `GET /auth/csrf`。附带 `X-Request-ID`。
* **错误**：401 清理会话并跳登录（带 `redirect`）；403/404/409/422 统一消息提示并展示后端 `request_id`。
* **租户隔离**：所有租户接口显式传 `tenant_id`（`/tenants/{tenant_id}/...`）；当前租户仅存本标签页 `sessionStorage`，不同标签页可用不同租户。
* **租户切换**：`AbortController` 取消旧租户在途请求 → 清空旧数据与勾选（`epoch` 递增触发页面重建）→ 重新拉 bootstrap 权限 → 按 `epoch` 丢弃迟到的旧响应。
* **权限**：菜单与按钮按 bootstrap `permissions` 显示（`v-permission` 或 `tenant.has()`），后端仍独立授权；权限不足页面跳 `/forbidden`。
* **异步任务**：202 返回 `task_id` 后轮询 `/tenants/{tenant_id}/tasks/{task_id}`，完成后刷新列表；切换租户后丢弃结果。

## 说明与已知限制

* 本工程未连接真实后端（后端并行开发中），页面在无后端时展示空列表/错误提示，不阻塞路由与构建。
* 服务器无邮件通道：平台租户详情抽屉的「负责人邀请」可生成一次性激活链接（`POST /platform/tenants/{id}/owner-invitation`），弹窗支持复制；http 部署下 Clipboard API 不可用时降级为选中输入框 + `execCommand`，最后提示手动 Ctrl+C。
* 商品编辑按契约 PATCH 商品字段；SKU 在新建时写入，编辑时不提交 SKU 变更（契约未定义 SKU 局部更新）。
* `vite.config.ts` 会把构建临时目录指向工程内 `.tmp-build/`：部分受限 Windows 环境禁止删除系统临时目录中的 esbuild 临时文件，会导致构建失败；该目录已被 `.gitignore` 忽略。
* 元素数量/金额沿用后端字符串，展示层仅格式化不改类型；JSON 原始数据用 `pre` 折叠展示。
