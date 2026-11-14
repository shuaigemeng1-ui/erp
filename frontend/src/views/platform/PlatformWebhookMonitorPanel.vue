<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh, Search } from '@element-plus/icons-vue'
import { describeApiError, isApiError } from '@/api/http'
import { opsApi } from '@/api/ops'
import { formatDateTime, stringOrUndefined } from '@/utils/format'
import type {
  OpsWebhookGatewayMetrics,
  OpsWebhookMetrics,
  OpsWebhookRedisMetrics,
  OpsWebhookRedisMemory,
  OpsWebhookStoreMetrics,
  OpsWebhookTenantMetrics,
  OpsWebhookTotals,
} from '@/types/ops'

const loading = ref(false)
const error = ref('')
const permissionError = ref(false)
const data = ref<OpsWebhookMetrics | null>(null)
const windowSeconds = ref(3600)
const tenantInput = ref('')
const storeInput = ref('')

const windowOptions = [
  { label: '近 15 分钟', value: 900 },
  { label: '近 1 小时', value: 3600 },
  { label: '近 24 小时', value: 86400 },
]

const totals = computed<OpsWebhookTotals>(() => data.value?.totals ?? {})
const gateway = computed<OpsWebhookGatewayMetrics | null>(() => data.value?.gateway ?? null)
const redis = computed<OpsWebhookRedisMetrics | null>(() => data.value?.redis ?? null)
const tenants = computed<OpsWebhookTenantMetrics[]>(() => data.value?.tenants ?? [])
const notes = computed(() => data.value?.notes ?? [])

interface DrillRow {
  key: string
  tenant_id: string
  tenant_name: string
  instance_name: string
  store_id: string
  store_name: string
  external_store_id: string
  events_received: number
  events_pending: number
  orders_pending: number
  oldest_pending_age_seconds: number | null
  orders_written: number
  refresh_completed: number
  orders_dead: number
  refresh_success_rate: number | null
  end_to_end_p95_ms: number | null
  backfill_watermark: string | null
  error: string
}

function numberOrZero(value: number | null | undefined): number {
  const parsed = Number(value ?? 0)
  return Number.isFinite(parsed) ? parsed : 0
}

function formatCount(value?: number | null): string {
  if (value === null || value === undefined) return '-'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed.toLocaleString('zh-CN') : '-'
}

function formatPercent(value?: number | null): string {
  if (value === null || value === undefined) return '-'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(1)}%` : '-'
}

function formatMs(value?: number | null): string {
  if (value === null || value === undefined) return '-'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${parsed.toFixed(0)} ms` : '-'
}

function formatAge(value?: number | null): string {
  if (value === null || value === undefined) return '-'
  const seconds = Math.max(0, Math.round(Number(value) || 0))
  if (seconds < 60) return `${seconds} 秒`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`
}

function isPendingStale(value?: number | null): boolean {
  return value !== null && value !== undefined && Number(value) > 120
}

function gatewayFieldLabel(field: string): string {
  const labels: Record<string, string> = {
    signature_failed: '验签失败',
    duplicate: '重复事件',
    p95_ms: '投递 P95',
  }
  return labels[field] ?? field
}

function redisMemoryText(memory?: OpsWebhookRedisMemory | null): string {
  if (!memory?.available) return memory?.error ? '不可用' : '-'
  const used = memory.used_memory_human || formatBytes(memory.used_memory_bytes)
  const max = memory.maxmemory_bytes ? memory.maxmemory_human || formatBytes(memory.maxmemory_bytes) : '不限'
  return `${used} / ${max}`
}

function formatBytes(value?: number | null): string {
  const bytes = Number(value ?? 0)
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

function storeRow(
  tenant: OpsWebhookTenantMetrics,
  store: OpsWebhookStoreMetrics,
): DrillRow {
  const storeId = store.store_id ?? ''
  return {
    key: `${tenant.tenant_id}-${storeId}`,
    tenant_id: tenant.tenant_id,
    tenant_name: tenant.tenant_name ?? '',
    instance_name: tenant.instance_name ?? '',
    store_id: storeId,
    store_name: store.store_name ?? '',
    external_store_id: store.external_store_id ?? '',
    events_received: store.events_received ?? 0,
    events_pending: store.events_pending ?? 0,
    orders_pending: store.orders_pending ?? 0,
    oldest_pending_age_seconds: store.oldest_pending_age_seconds ?? null,
    orders_written: store.orders_written ?? 0,
    refresh_completed: store.refresh_completed ?? 0,
    orders_dead: store.orders_dead ?? 0,
    refresh_success_rate: store.refresh_success_rate ?? null,
    end_to_end_p95_ms: store.end_to_end_p95_ms ?? null,
    backfill_watermark: store.backfill_watermark ?? null,
    error: '',
  }
}

function tenantRow(tenant: OpsWebhookTenantMetrics): DrillRow {
  return {
    key: `tenant-${tenant.tenant_id}`,
    tenant_id: tenant.tenant_id,
    tenant_name: tenant.tenant_name ?? '',
    instance_name: tenant.instance_name ?? '',
    store_id: '',
    store_name: '（全部店铺）',
    external_store_id: '',
    events_received: tenant.events_received ?? 0,
    events_pending: tenant.events_pending ?? 0,
    orders_pending: tenant.orders_pending ?? 0,
    oldest_pending_age_seconds: tenant.oldest_pending_age_seconds ?? null,
    orders_written: tenant.orders_written ?? 0,
    refresh_completed: tenant.refresh_completed ?? 0,
    orders_dead: tenant.orders_dead ?? 0,
    refresh_success_rate: tenant.refresh_success_rate ?? null,
    end_to_end_p95_ms: tenant.end_to_end_p95_ms ?? null,
    backfill_watermark: tenant.backfill_watermark ?? null,
    error: tenant.error ?? '',
  }
}

const drillRows = computed<DrillRow[]>(() => {
  const rows: DrillRow[] = []
  for (const tenant of tenants.value) {
    if (tenant.error) {
      rows.push({ ...tenantRow(tenant), error: tenant.error })
      continue
    }
    const stores = tenant.stores ?? []
    if (stores.length === 0) {
      rows.push(tenantRow(tenant))
      continue
    }
    for (const store of stores) rows.push(storeRow(tenant, store))
  }
  return rows
})

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  permissionError.value = false
  try {
    data.value = await opsApi.webhookMetrics({
      window_seconds: windowSeconds.value,
      tenant_id: stringOrUndefined(tenantInput.value),
      store_id: stringOrUndefined(storeInput.value),
    })
  } catch (cause) {
    data.value = null
    if (isApiError(cause) && (cause.status === 403 || cause.status === 401)) {
      permissionError.value = true
      error.value = '需要平台管理员权限才能查看 Webhook 指标'
    } else {
      error.value = describeApiError(cause)
    }
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <el-card shadow="never" class="page-card webhook-card">
    <div class="table-toolbar">
      <div class="section-title">
        TikTok Webhook
        <span class="text-muted">事件接收 → 订单待刷新 → 订单已写入</span>
      </div>
      <div class="toolbar-actions">
        <el-select v-model="windowSeconds" style="width: 130px" @change="load()">
          <el-option
            v-for="option in windowOptions"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
        <el-input
          v-model="tenantInput"
          placeholder="租户 ID"
          clearable
          style="width: 110px"
          @keyup.enter="load()"
        />
        <el-input
          v-model="storeInput"
          placeholder="店铺 ID"
          clearable
          style="width: 110px"
          @keyup.enter="load()"
        />
        <el-button type="primary" :icon="Search" @click="load()">查询</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="load()">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="error"
      class="webhook-error"
      type="error"
      :title="error"
      :closable="false"
      show-icon
    >
      <el-button text type="primary" :loading="loading" @click="load()">重试</el-button>
    </el-alert>

    <div v-else v-loading="loading" class="webhook-body">
      <template v-if="tenants.length">
        <div class="webhook-flow">
          <div class="webhook-flow-card">
            <div class="webhook-flow-label">事件已接收</div>
            <div class="webhook-flow-value">{{ formatCount(totals.events_received) }}</div>
            <div class="webhook-flow-sub">
              窗口内 webhook_events；待处理 {{ formatCount(totals.events_pending) }}
            </div>
          </div>
          <div class="webhook-flow-arrow">→</div>
          <div class="webhook-flow-card">
            <div class="webhook-flow-label">订单待刷新</div>
            <div class="webhook-flow-value">{{ formatCount(totals.orders_pending) }}</div>
            <div
              class="webhook-flow-sub"
              :class="{ 'webhook-danger-text': isPendingStale(totals.oldest_pending_age_seconds) }"
            >
              最老待刷新 {{ formatAge(totals.oldest_pending_age_seconds) }}
              <span v-if="isPendingStale(totals.oldest_pending_age_seconds)">（超过 120 秒）</span>
            </div>
          </div>
          <div class="webhook-flow-arrow">→</div>
          <div class="webhook-flow-card">
            <div class="webhook-flow-label">订单已写入</div>
            <div class="webhook-flow-value">{{ formatCount(totals.orders_written) }}</div>
            <div class="webhook-flow-sub">
              刷新完成 {{ formatCount(totals.refresh_completed) }}；dead
              {{ formatCount(totals.orders_dead) }}
            </div>
          </div>
        </div>

        <div class="webhook-secondary-grid">
          <div class="webhook-metric">
            <div class="webhook-metric-label">刷新成功率</div>
            <div class="webhook-metric-value">{{ formatPercent(totals.refresh_success_rate) }}</div>
            <div class="webhook-metric-sub">完成 /（完成 + dead）</div>
          </div>
          <div class="webhook-metric">
            <div class="webhook-metric-label">端到端 P95</div>
            <div class="webhook-metric-value">{{ formatMs(totals.end_to_end_p95_ms) }}</div>
            <div class="webhook-metric-sub">
              样本 {{ formatCount(totals.end_to_end_samples) }}；阈值 120 秒
            </div>
          </div>
          <div class="webhook-metric">
            <div class="webhook-metric-label">最近补拉水位</div>
            <div class="webhook-metric-value webhook-metric-small">
              {{ totals.backfill_watermark ? formatDateTime(totals.backfill_watermark) : '-' }}
            </div>
            <div class="webhook-metric-sub">order_sync_checkpoints.watermark</div>
          </div>
          <div class="webhook-metric">
            <div class="webhook-metric-label">Redis 发布失败</div>
            <div
              class="webhook-metric-value"
              :class="{ 'webhook-danger-text': numberOrZero(totals.outbox_failed) > 0 }"
            >
              {{ formatCount(totals.outbox_failed) }}
            </div>
            <div class="webhook-metric-sub">租户 outbox failed 代理指标</div>
          </div>
        </div>

        <div class="webhook-columns">
          <div class="webhook-panel">
            <div class="mini-title">
              Gateway 事件
              <span class="text-muted">
                {{ gateway?.windowed ? '窗口统计' : '累计统计' }}
              </span>
            </div>
            <template v-if="gateway">
              <el-alert
                v-if="!gateway.available || gateway.source !== 'gateway_admin_webhook_stats'"
                type="warning"
                :closable="false"
                show-icon
                :title="gateway.reason || '网关 Webhook 统计不可用，仅展示健康信息'"
              />
              <div class="webhook-gateway-grid">
                <div>
                  <span class="webhook-label">已接收</span>
                  <span class="mono">{{ formatCount(gateway.received_total) }}</span>
                </div>
                <div>
                  <span class="webhook-label">验签失败</span>
                  <span class="mono">{{ formatCount(gateway.signature_failed) }}</span>
                </div>
                <div>
                  <span class="webhook-label">重复事件</span>
                  <span class="mono">{{ formatCount(gateway.duplicate) }}</span>
                </div>
                <div>
                  <span class="webhook-label">pending</span>
                  <span class="mono">{{ formatCount(gateway.pending) }}</span>
                </div>
                <div>
                  <span class="webhook-label">delivered</span>
                  <span class="mono">{{ formatCount(gateway.delivered) }}</span>
                </div>
                <div>
                  <span class="webhook-label">dead</span>
                  <span class="mono">{{ formatCount(gateway.dead) }}</span>
                </div>
                <div>
                  <span class="webhook-label">unroutable</span>
                  <span class="mono">{{ formatCount(gateway.unroutable) }}</span>
                </div>
                <div>
                  <span class="webhook-label">窗口 unroutable</span>
                  <span class="mono">{{ formatCount(gateway.unroutable_recent) }}</span>
                </div>
                <div>
                  <span class="webhook-label">最老 pending</span>
                  <span class="mono">{{ formatAge(gateway.oldest_pending_age_seconds) }}</span>
                </div>
                <div>
                  <span class="webhook-label">成功率</span>
                  <span class="mono">{{ formatPercent(gateway.success_rate) }}</span>
                </div>
                <div>
                  <span class="webhook-label">最近投递耗时</span>
                  <span class="mono">{{ formatMs(gateway.last_delivery_latency_ms) }}</span>
                </div>
                <div>
                  <span class="webhook-label">投递 P95</span>
                  <span class="mono">{{ formatMs(gateway.p95_ms) }}</span>
                </div>
              </div>
              <div v-if="gateway.unavailable_fields?.length" class="webhook-unavailable">
                <span class="text-muted">网关未提供：</span>
                <el-tag
                  v-for="field in gateway.unavailable_fields"
                  :key="field"
                  type="info"
                  size="small"
                >
                  {{ gatewayFieldLabel(field) }}
                </el-tag>
              </div>
              <div v-if="gateway.unavailable_reason" class="webhook-note text-muted">
                {{ gateway.unavailable_reason }}
              </div>
            </template>
          </div>

          <div class="webhook-panel">
            <div class="mini-title">Redis / 发布</div>
            <template v-if="redis">
              <div class="webhook-redis-grid">
                <div>
                  <span class="webhook-label">缓存 Redis</span>
                  <span class="mono">{{ redisMemoryText(redis.cache) }}</span>
                </div>
                <div>
                  <span class="webhook-label">队列 Redis</span>
                  <span class="mono">{{ redisMemoryText(redis.queue) }}</span>
                </div>
                <div>
                  <span class="webhook-label">发布失败</span>
                  <span class="mono">{{ formatCount(redis.publish_failed) }}</span>
                </div>
                <div>
                  <span class="webhook-label">429 限流</span>
                  <span class="mono">—</span>
                </div>
              </div>
              <div class="webhook-note text-muted">
                {{ redis.publish_failed_source || '发布失败来自租户 outbox 代理指标' }}；{{
                  redis.rate_limited_reason || '429 未单独计数'
                }}
              </div>
            </template>
          </div>
        </div>

        <div class="section-head webhook-table-head">
          租户 / 店铺下钻
          <span class="text-muted">（事件已接收不等于订单已写入）</span>
        </div>
        <el-table :data="drillRows" border size="small" row-key="key">
          <el-table-column label="租户 / 实例" min-width="170">
            <template #default="{ row }">
              <div>{{ row.tenant_name || `租户 ${row.tenant_id}` }}</div>
              <div class="text-muted">{{ row.instance_name || '-' }}</div>
            </template>
          </el-table-column>
          <el-table-column label="店铺" min-width="140">
            <template #default="{ row }">
              <div>{{ row.store_name || '-' }}</div>
              <div v-if="row.external_store_id" class="text-muted mono">
                {{ row.external_store_id }}
              </div>
            </template>
          </el-table-column>
          <el-table-column label="事件接收" width="95">
            <template #default="{ row }">{{ formatCount(row.events_received) }}</template>
          </el-table-column>
          <el-table-column label="待刷新" width="85">
            <template #default="{ row }">{{ formatCount(row.orders_pending) }}</template>
          </el-table-column>
          <el-table-column label="最老待刷新" width="125">
            <template #default="{ row }">
              <span :class="{ 'webhook-danger-text': isPendingStale(row.oldest_pending_age_seconds) }">
                {{ formatAge(row.oldest_pending_age_seconds) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="已写入" width="85">
            <template #default="{ row }">{{ formatCount(row.orders_written) }}</template>
          </el-table-column>
          <el-table-column label="刷新成功率" width="105">
            <template #default="{ row }">{{ formatPercent(row.refresh_success_rate) }}</template>
          </el-table-column>
          <el-table-column label="端到端 P95" width="105">
            <template #default="{ row }">{{ formatMs(row.end_to_end_p95_ms) }}</template>
          </el-table-column>
          <el-table-column label="补拉水位" width="170">
            <template #default="{ row }">{{ formatDateTime(row.backfill_watermark) }}</template>
          </el-table-column>
          <el-table-column label="状态" min-width="150">
            <template #default="{ row }">
              <el-tag v-if="row.error" type="danger" size="small" disable-transitions>
                {{ row.error }}
              </el-tag>
              <span v-else-if="row.orders_dead" class="text-danger">
                dead {{ formatCount(row.orders_dead) }}
              </span>
              <span v-else class="text-muted">正常</span>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="当前筛选窗口内暂无 Webhook 数据" :image-size="60" />
          </template>
        </el-table>

        <div v-if="notes.length" class="webhook-notes text-muted">
          <div v-for="note in notes" :key="note">· {{ note }}</div>
        </div>
      </template>
      <el-empty
        v-else-if="!loading"
        description="暂无 Webhook 指标（尚无租户实例或接口未返回数据）"
        :image-size="60"
      />
    </div>
  </el-card>
</template>

<style scoped>
.webhook-card {
  margin-bottom: 12px;
}

.webhook-card .section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.webhook-error {
  margin-bottom: 8px;
}

.webhook-body {
  min-height: 80px;
}

.webhook-flow {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: stretch;
  gap: 8px;
  margin-bottom: 10px;
}

.webhook-flow-card {
  padding: 12px;
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.webhook-flow-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.webhook-flow-value {
  margin-top: 4px;
  font-size: 26px;
  font-weight: 600;
  line-height: 1.2;
}

.webhook-flow-sub {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.webhook-flow-arrow {
  display: flex;
  align-items: center;
  color: var(--el-text-color-secondary);
  font-size: 20px;
}

.webhook-secondary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 10px;
}

.webhook-metric {
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.webhook-metric-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.webhook-metric-value {
  margin-top: 4px;
  font-size: 20px;
  font-weight: 600;
  line-height: 1.2;
}

.webhook-metric-value.webhook-metric-small {
  font-size: 13px;
  font-weight: 500;
}

.webhook-metric-sub {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 11px;
}

.webhook-columns {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
  gap: 10px;
  margin-bottom: 4px;
}

.webhook-panel {
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.mini-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
}

.mini-title .text-muted {
  font-weight: 400;
}

.webhook-gateway-grid,
.webhook-redis-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  font-size: 12px;
}

.webhook-gateway-grid > div,
.webhook-redis-grid > div {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 8px;
  background: var(--el-fill-color-lighter);
  border-radius: 4px;
}

.webhook-label {
  color: var(--el-text-color-secondary);
}

.webhook-unavailable {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
  margin-top: 8px;
  font-size: 12px;
}

.webhook-note {
  margin-top: 6px;
  font-size: 11px;
  line-height: 1.5;
}

.webhook-table-head {
  margin-top: 16px;
}

.webhook-table-head .text-muted {
  font-weight: 400;
  font-size: 12px;
}

.webhook-danger-text {
  color: var(--el-color-danger);
}

.text-danger {
  color: var(--el-color-danger);
}

.webhook-notes {
  margin-top: 8px;
  font-size: 11px;
  line-height: 1.6;
}

@media (max-width: 1100px) {
  .webhook-secondary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .webhook-columns {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 900px) {
  .webhook-flow {
    grid-template-columns: minmax(0, 1fr);
  }

  .webhook-flow-arrow {
    display: none;
  }
}
</style>
