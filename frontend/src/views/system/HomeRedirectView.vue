<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { resolveHomePath } from '@/router/home'

const router = useRouter()

onMounted(async () => {
  await router.replace(resolveHomePath())
})
</script>

<template>
  <div v-loading="true" element-loading-text="正在进入..." style="height: 60vh" />
</template>
