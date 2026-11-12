<script setup lang="ts">
import { reactive } from 'vue'
import { Refresh, Search } from '@element-plus/icons-vue'
import { tenantApi } from '@/api/tenant'
import { useTenantStore } from '@/stores/tenant'
import { useTableQuery } from '@/composables/useTableQuery'
import PaginationBar from '@/components/PaginationBar.vue'
import { formatDateTime, stringOrUndefined, textOrDash, truncate } from '@/utils/format'
import type { AuditLog } from '@/types'

const tenant = useTenantStore()
const tid = (): string => tenant.requireTenantId()

const query = reactive({
  action: '',
  q: '',
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
} = useTableQuery<AuditLog>((params) =>
  tenantApi.auditLogs(tid(), {
    page: params.page,
    page_size: params.page_size,
    action: stringOrUndefined(query.action),
    q: stringOrUndefined(query.q),
  }),
)
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="filter-bar">
      <el-input v-model="query.action" placeholder="操作编码" clearable style="width: 210px" @keyup.enter="search()" />
      <el-input v-model="query.q" placeholder="摘要关键词" clearable style="width: 200px" @keyup.enter="search()" />
      <el-button type="primary" :icon="Search" @click="search()">查询</el-button>
      <el-button :icon="Refresh" @click="reload()">刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" border size="small" row-key="id">
      <el-table-column label="操作人" width="150">
        <template #default="{ row }">{{ row.actor_name || '未知用户' }}</template>
      </el-table-column>
      <el-table-column prop="action" label="操作" min-width="190" show-overflow-tooltip />
      <el-table-column label="对象" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ row.resource_type }} / {{ textOrDash(row.resource_id) }}</template>
      </el-table-column>
      <el-table-column label="摘要" min-width="260" show-overflow-tooltip>
        <template #default="{ row }">{{ truncate(row.summary, 90) }}</template>
      </el-table-column>
      <el-table-column prop="ip" label="IP" width="130" />
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无操作日志" :image-size="72" />
      </template>
    </el-table>

    <PaginationBar
      :total="total"
      :page="page"
      :page-size="pageSize"
      @page-change="handlePageChange"
      @size-change="handleSizeChange"
    />
  </el-card>
</template>
