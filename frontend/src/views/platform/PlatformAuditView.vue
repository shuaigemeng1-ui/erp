<script setup lang="ts">
import { reactive, ref } from 'vue'
import { Refresh, Search } from '@element-plus/icons-vue'
import { platformApi } from '@/api/platform'
import { useTableQuery } from '@/composables/useTableQuery'
import PaginationBar from '@/components/PaginationBar.vue'
import IdCopy from '@/components/IdCopy.vue'
import JsonBlock from '@/components/JsonBlock.vue'
import { formatDateTime, stringOrUndefined, textOrDash, truncate } from '@/utils/format'
import type { PlatformAuditLog } from '@/types'

const query = reactive({
  action: '',
  tenant_id: '',
})

const {
  rows,
  total,
  page,
  pageSize,
  loading,
  search,
  reload,
  handlePageChange,
  handleSizeChange,
} = useTableQuery<PlatformAuditLog>((params) =>
  platformApi.auditLogs({
    page: params.page,
    page_size: params.page_size,
    action: stringOrUndefined(query.action),
    tenant_id: stringOrUndefined(query.tenant_id),
  }),
)

const payloadVisible = ref(false)
const currentPayload = ref<unknown>(null)
const currentAction = ref('')

function openPayload(row: PlatformAuditLog): void {
  currentAction.value = row.action
  currentPayload.value = row.payload ?? null
  payloadVisible.value = true
}
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="filter-bar">
      <el-input v-model="query.action" placeholder="操作编码" clearable style="width: 220px" @keyup.enter="search()" />
      <el-input v-model="query.tenant_id" placeholder="租户 ID" clearable style="width: 180px" @keyup.enter="search()" />
      <el-button type="primary" :icon="Search" @click="search()">查询</el-button>
      <el-button :icon="Refresh" @click="reload()">刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" border size="small" row-key="id">
      <el-table-column label="ID" width="100">
        <template #default="{ row }"><IdCopy :id="row.id" /></template>
      </el-table-column>
      <el-table-column prop="action" label="操作" min-width="160" show-overflow-tooltip />
      <el-table-column label="对象" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ row.resource_type }} / {{ row.resource_id ?? '-' }}</template>
      </el-table-column>
      <el-table-column label="租户" width="110">
        <template #default="{ row }"><IdCopy :id="row.tenant_id" /></template>
      </el-table-column>
      <el-table-column label="操作" width="110">
        <template #default="{ row }"><IdCopy :id="row.actor_user_id" /></template>
      </el-table-column>
      <el-table-column label="摘要" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ truncate(row.summary, 80) }}</template>
      </el-table-column>
      <el-table-column prop="ip" label="IP" width="130" />
      <el-table-column label="request_id" width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="mono">{{ textOrDash(row.request_id) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button text type="primary" size="small" @click="openPayload(row as PlatformAuditLog)">详情</el-button>
        </template>
      </el-table-column>
    </el-table>

    <PaginationBar
      :total="total"
      :page="page"
      :page-size="pageSize"
      @page-change="handlePageChange"
      @size-change="handleSizeChange"
    />
  </el-card>

  <el-drawer v-model="payloadVisible" :title="`审计详情：${currentAction}`" size="560px">
    <JsonBlock :value="currentPayload" empty-text="该记录没有 payload" />
  </el-drawer>
</template>
