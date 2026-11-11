<script setup lang="ts">
import { computed } from 'vue'
import { prettyJson } from '@/utils/format'

const props = defineProps<{
  value?: unknown
  emptyText?: string
}>()

const text = computed(() => prettyJson(props.value))
</script>

<template>
  <pre v-if="text" class="json-block">{{ text }}</pre>
  <span v-else class="text-muted">{{ emptyText ?? '无数据' }}</span>
</template>
