# 独立集成网关实际部署记录（过渡方案）

- 日期：2026-11-13（部署与验证）
- 环境：中心机 `121.37.45.132`（网关与中心同机，**过渡方案**）
- 域名：`gateway.alienyan.store`
- 记录角色：deploy-qa（task-39，**只整理文档与只读取证，未操作生产**）；实际部署由试点流程执行
- 关联：`docs/deployment-modes.md`、`docs/acceptance.md`、`docs/batch4-tenant-onboarding.md`、
  部署包 `deploy/gateway/README.md`

> ⚠️ 这是**过渡部署**：网关容器跑在中心机，与中心共享主机故障域，不满足“中心故障时网关仍活”。
> 正式方案仍需独立 VPS/独立故障域；迁移步骤与风险见第 7 节。

## 1. 实际拓扑

```text
TikTok / 浏览器
      │ HTTPS
      ▼
gateway.alienyan.store (DNS A → 121.37.45.132)
      │ Caddy TLS（证书已签发）
      ▼
127.0.0.1:8101  →  container erp-gateway-v2（镜像 erp-gateway:7fbce73）
                     └─ volume erp-gateway-v2-data:/data/gateway.db（SQLite）
      │ 实例令牌 / OAuth state / refresh lock
      ▼
租户实例 b485bee（https://zuhu.alienyan.store）
      │
      └─ 中心库 tenant_instances id=1 / tenant_id=3 / independent / migrated
```

## 2. 部署事实

| 项目 | 实际值 |
| --- | --- |
| DNS | `gateway.alienyan.store` → `121.37.45.132` |
| TLS | Caddy 已签发证书；中心机新增 `gateway.alienyan.store` server block |
| 反向代理 | `127.0.0.1:8101` |
| 新容器 | `erp-gateway-v2` |
| 镜像 | `erp-gateway:7fbce73` |
| 数据卷 | `erp-gateway-v2-data` → `/data/gateway.db` |
| 重启策略 | `unless-stopped` |
| 健康检查 | `https://gateway.alienyan.store/healthz` → **200** |
| 旧容器 | `erp-gateway` 已停止（未删除） |
| 旧卷 | `erp-gateway-data` 保留（回滚用） |
| 备份 | `/opt/erp-gateway/backups/gateway-data-20261113T103136Z.tgz` |
| 旧端口 | `8100` 对应旧容器，已停但未删除/清理 |

> 注意：标准部署包 `deploy/gateway/` 默认容器/卷/端口为 `erp-gateway` / `erp_gateway_data` / `8100`；
> 本次生产实际使用 `erp-gateway-v2` / `erp-gateway-v2-data` / `8101`，属于滚动替换的临时命名。
> 后续操作必须按实际容器/卷名，不要照抄文档默认值。

## 3. 网关实例与渠道应用

| 项目 | 实际值 |
| --- | --- |
| 网关实例 | `id=1`、`tenant_id=3`、`status=active`、`verified` |
| 渠道应用数 | `channel_apps=1`（TikTok） |
| TikTok app_key | `6l18o3tqf4a6v`（从旧 SQLite 迁移） |
| TikTok 回调 | `https://gateway.alienyan.store/api/v1/gateway/oauth/tiktok/callback` |
| 实例令牌 | 已发放并仅配置到租户 `b485bee` 的 `GATEWAY_INSTANCE_TOKEN` |

中心库记录：

- `tenant_instances`：`id=1`、`tenant_id=3`、`target_version=b485bee`、`deployment_mode=independent`、
  `migration_status=migrated`；
- `release_versions`：`b485bee`，`build_status=verified`。

## 4. 租户配置（b485bee）

| 变量 | 值/状态 |
| --- | --- |
| `GATEWAY_BASE_URL` | `https://gateway.alienyan.store` |
| `GATEWAY_PUBLIC_URL` | `https://gateway.alienyan.store`（网关侧公网地址） |
| `GATEWAY_INSTANCE_ID` | `1` |
| `GATEWAY_INSTANCE_TOKEN` | 已配置（不写入文档） |
| `GATEWAY_CHANNEL_CONFIG` | `true` |
| `GATEWAY_OAUTH_RELAY_ENABLED` | `true` |
| `GATEWAY_REFRESH_LOCK_ENABLED` | `true` |
| `GATEWAY_ADMIN_TOKEN` | **已移除**（租户不再持有平台管理员令牌） |
| `GATEWAY_LEGACY_ADMIN_ENABLED` | `false` |

说明：租户镜像 `b485bee` 已部署并完成基础健康检查；网关仅保存渠道应用/实例/OAuth
state/刷新锁等集成状态，不保存租户业务数据。

## 5. 验证证据（2026-11-13）

| 检查 | 结果 |
| --- | --- |
| 网关健康 | `https://gateway.alienyan.store/healthz` → **200** |
| 实例状态 | `id=1`、`tenant_id=3`、`active`、`verified` |
| 渠道应用 | `channel_apps=1`（TikTok） |
| TikTok 授权发起 | 实际走网关 state；租户 API 日志 `POST /instance/oauth/states` → **200** |
| 租户 OAuth 回调 | 租户回调 → **302**（按预期重定向/继续） |
| 网关 OAuth 回调 | `GET .../gateway/oauth/tiktok/callback`（非法 code）→ **400**，非 404；说明网关回调路由已生效 |
| 旧网关 | `erp-gateway` 已停止；旧卷保留 |

## 6. 回滚与保留

回滚目标：恢复到旧 `erp-gateway` + `erp-gateway-data`/`8100` 链路。

1. 保留现场：不要删除旧容器 `erp-gateway`、旧卷 `erp-gateway-data`、备份包
   `/opt/erp-gateway/backups/gateway-data-20261113T103136Z.tgz`；
2. 恢复 Caddy upstream 到 `127.0.0.1:8100`（或重启旧容器并确认其健康）；
3. 如新链异常，停 `erp-gateway-v2`（数据卷保留），启动旧 `erp-gateway`；
4. 租户侧按需恢复旧 `GATEWAY_ADMIN_TOKEN`/`GATEWAY_LEGACY_ADMIN_ENABLED=true` 前必须评估安全风险并 Lead 审批；
5. 回滚后验证 `/healthz`、实例 `active`、TikTok 授权链路；确认无双重写/重复刷新后再清理新资源。

> 旧卷/旧容器是回滚资产，**在观察期结束且 Lead 确认前不得删除**；删除前先备份。

## 7. 风险与待办

1. **同机部署，不满足故障隔离**：网关与中心同在 `121.37.45.132`；中心主机故障会同时影响网关，
   仅是过渡方案。正式方案需要独立 VPS/独立故障域，并更新 DNS/Caddy/实例回调。
2. **TikTok 开发者后台 Redirect URI 仍需修改**：当前实际回调已切到 `https://gateway.alienyan.store/api/v1/gateway/oauth/tiktok/callback`，
   但 TikTok 开发者后台的 Redirect URI 变更属外部权限，**尚未执行**；未改前授权可能失败/回退。
3. **旧 8100 未删除**：旧容器 `erp-gateway` 已停但未删除，旧端口 8100 仍占位；观察期后按回滚策略清理。
4. **旧卷保留**：`erp-gateway-data` 保留回滚，需纳入备份/密钥管理，避免泄露渠道凭据。
5. **监控/告警**：网关健康与证书到期需纳入监控；当前仅人工健康检查。
6. **备份恢复演练**：网关 SQLite 备份已生成，但独立恢复演练未执行；恢复需同时恢复 `GATEWAY_SECRET_KEY`。
7. **标准部署包与实际不一致**：`deploy/gateway/` 默认 8100/erp-gateway/erp_gateway_data；本次实际为
   8101/erp-gateway-v2/erp-gateway-v2-data。后续发布需统一命名或明确覆盖值，避免误操作。

## 8. 只读运维检查（示例，不修改生产）

```bash
# 健康
curl -fsS https://gateway.alienyan.store/healthz

# 容器/镜像/重启策略/端口/挂载
docker inspect erp-gateway-v2 --format '{{.Config.Image}} {{.HostConfig.RestartPolicy.Name}} {{json .NetworkSettings.Ports}} {{json .Mounts}}'

# 旧容器状态（应为 exited；观察期保留）
docker inspect erp-gateway --format '{{.State.Status}} {{.Config.Image}}'

# 卷（新/旧均保留）
docker volume ls | grep -E 'erp-gateway(-v2)?-data'

# 日志（不输出密钥）
docker logs --tail 100 erp-gateway-v2

# 备份文件
ls -l /opt/erp-gateway/backups/gateway-data-20261113T103136Z.tgz
```

Caddy 配置变更后先 `caddy validate`/`caddy reload`（按中心机实际方式），并确认 TLS 证书状态。

## 9. 验收清单

- [x] DNS `gateway.alienyan.store` → `121.37.45.132`
- [x] Caddy TLS 证书签发，反代 `127.0.0.1:8101`
- [x] `erp-gateway-v2`（镜像 `erp-gateway:7fbce73`）运行、`unless-stopped`、卷 `erp-gateway-v2-data`
- [x] `/healthz` 200
- [x] 实例 id=1 / tenant_id=3 / active / verified；channel_apps=1
- [x] TikTok app_key 迁移、回调改为网关地址
- [x] 租户 b485bee 配置实例 token、relay/lock=true，移除 admin token
- [x] TikTok 授权走网关 state（`/instance/oauth/states` 200）；租户回调 302、网关回调 400
- [x] 旧容器停止、旧卷保留、网关数据备份
- [ ] 中心故障时网关独立存活（**不满足**，同机过渡）
- [ ] TikTok 开发者后台 Redirect URI 改为网关回调（外部待办）
- [ ] 旧 8100/旧容器观察期后清理（待 Lead 决策）

## 10. 关联文档

- 部署形态：`docs/deployment-modes.md`
- 租户接入：`docs/batch4-tenant-onboarding.md`
- 验收状态：`docs/acceptance.md`
- 标准网关部署包：`deploy/gateway/README.md`（默认命名与本次实际不同，操作前先核对）
- 批次 3 备份/恢复：`docs/backup-restore.md`
