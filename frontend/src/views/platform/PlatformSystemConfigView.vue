<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PlatformSystemSettingsView from './PlatformSystemSettingsView.vue'
import PlatformChannelAppsView from './PlatformChannelAppsView.vue'
import PlatformEmailView from './PlatformEmailView.vue'
import PlatformPlansView from './PlatformPlansView.vue'

type ConfigTab = 'storage' | 'channels' | 'email' | 'plans'

const route = useRoute()
const router = useRouter()

function tabFromQuery(): ConfigTab {
  if (route.query.tab === 'channels') return 'channels'
  if (route.query.tab === 'email') return 'email'
  if (route.query.tab === 'plans') return 'plans'
  return 'storage'
}

const activeTab = ref<ConfigTab>(tabFromQuery())
const activeLabel = computed(() => {
  if (activeTab.value === 'channels') return '渠道应用'
  if (activeTab.value === 'email') return '邮件服务'
  if (activeTab.value === 'plans') return '套餐管理'
  return '对象存储'
})

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
  <div class="system-config-page">
    <div class="system-config-head">
      <div>
        <h2>系统配置</h2>
        <p>对象存储与渠道应用统一在这里配置，保存后立即生效。</p>
      </div>
      <el-tag type="info" effect="plain">{{ activeLabel }}</el-tag>
    </div>

    <el-tabs v-model="activeTab" class="system-config-tabs">
      <el-tab-pane label="对象存储" name="storage" lazy>
        <PlatformSystemSettingsView />
      </el-tab-pane>
      <el-tab-pane label="渠道应用" name="channels" lazy>
        <PlatformChannelAppsView />
      </el-tab-pane>
      <el-tab-pane label="邮件服务" name="email" lazy>
        <PlatformEmailView />
      </el-tab-pane>
      <el-tab-pane label="套餐管理" name="plans" lazy>
        <PlatformPlansView />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.system-config-page {
  min-width: 0;
}

.system-config-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 8px;
}

.system-config-head h2 {
  margin: 0;
  font-size: 20px;
  line-height: 1.4;
}

.system-config-head p {
  margin: 4px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.system-config-tabs :deep(.el-tabs__header) {
  margin-bottom: 12px;
}
</style>
