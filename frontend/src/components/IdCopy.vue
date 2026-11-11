<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { CopyDocument } from '@element-plus/icons-vue'

const props = defineProps<{
  id?: string | null
}>()

async function copy(): Promise<void> {
  if (!props.id) return
  try {
    await navigator.clipboard.writeText(props.id)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择')
  }
}
</script>

<template>
  <span class="id-cell">
    <span class="mono">{{ id || '-' }}</span>
    <el-button v-if="id" text size="small" :icon="CopyDocument" @click="copy" />
  </span>
</template>
