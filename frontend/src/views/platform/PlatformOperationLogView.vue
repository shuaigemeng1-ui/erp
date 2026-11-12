<script setup lang="ts">
import { reactive } from 'vue'
import { Refresh, Search } from '@element-plus/icons-vue'
import { platformApi } from '@/api/platform'
import { useTableQuery } from '@/composables/useTableQuery'
import PaginationBar from '@/components/PaginationBar.vue'
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

function actorLabel(row: PlatformAuditLog): string {
  return row.actor_name || (row.actor_user_id ? `用户 ${row.actor_user_id}` : '系统')
}
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="filter-bar">
      <el-input v-model="query.action" placeholder="操作编码" clearable style="width: 210px" @keyup.enter="search()" />
      <el-input v-model="query.tenant_id" placeholder="租户 ID" clearable style="width: 160px" @keyup.enter="search()" />
      <el-button type="primary" :icon="Search" @click="search()">查询</el-button>
      <el-button :icon="Refresh" @click="reload()">刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" border size="small" row-key="id">
      <el-table-column label="操作人" width="150">
        <template #default="{ row }">{{ actorLabel(row as PlatformAuditLog) }}</template>
      </el-table-column>
      <el-table-column label="操作" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.action_label || row.action }}
        </template>
      </el-table-column>
      <el-table-column label="对象" min-width="170" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.resource_type }} / {{ textOrDash(row.resource_id) }}
        </template>
      </el-table-column>
      <el-table-column label="租户" width="110">
        <template #default="{ row }">{{ textOrDash(row.tenant_id) }}</template>
      </el-table-column>
      <el-table-column label="摘要" min-width="240" show-overflow-tooltip>
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
