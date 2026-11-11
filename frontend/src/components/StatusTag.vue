<script setup lang="ts">
import { computed } from 'vue'
import { metaOf } from '@/utils/dict'
import type { StatusMeta } from '@/utils/dict'

const props = defineProps<{
  status?: string | null
  map?: Record<string, StatusMeta>
}>()

const meta = computed<StatusMeta>(() => {
  if (props.map) {
    const value = props.status ?? ''
    return props.map[value] ?? { label: value || '-', type: 'info' }
  }
  return metaOf(props.status)
})
</script>

<template>
  <el-tag :type="meta.type" size="small" disable-transitions>{{ meta.label }}</el-tag>
</template>
