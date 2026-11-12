<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import TenantOperationLogView from './TenantOperationLogView.vue'
import AuditLogsView from './AuditLogsView.vue'

type LogTab = 'operation' | 'audit'

const route = useRoute()
const router = useRouter()

function tabFromQuery(): LogTab {
  return route.query.tab === 'audit' ? 'audit' : 'operation'
}

const activeTab = ref<LogTab>(tabFromQuery())
const activeLabel = computed(() => (activeTab.value === 'audit' ? '审计日志' : '操作日志'))

watch(activeTab, (tab) => {
  if (route.query.tab === tab) return
  void router.replace({ query: { ...route.query, tab } })
})

watch(
  () => route.query.tab,
  () => {
    const next = tabFromQuery()
    if (next !== activeTab.value) activeTab.value = next
  },
)
</script>

<template>
  <div class="log-page">
    <div class="log-head">
      <div>
        <h2>日志</h2>
        <p>租户操作记录与审计明细。</p>
      </div>
      <el-tag type="info" effect="plain">{{ activeLabel }}</el-tag>
    </div>

    <el-tabs v-model="activeTab" class="log-tabs">
      <el-tab-pane label="操作日志" name="operation" lazy>
        <TenantOperationLogView />
      </el-tab-pane>
      <el-tab-pane label="审计日志" name="audit" lazy>
        <AuditLogsView />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.log-page {
  min-width: 0;
}

.log-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 8px;
}

.log-head h2 {
  margin: 0;
  font-size: 20px;
  line-height: 1.4;
}

.log-head p {
  margin: 4px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.log-tabs :deep(.el-tabs__header) {
  margin-bottom: 12px;
}
</style>
