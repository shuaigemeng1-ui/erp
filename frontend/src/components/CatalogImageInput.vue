<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { catalogApi } from '@/api/catalog'
import { useTenantStore } from '@/stores/tenant'
const props = withDefaults(defineProps<{ modelValue: string[]; multiple?: boolean; disabled?: boolean }>(), { multiple: false, disabled: false })
const emit = defineEmits<{ 'update:modelValue': [value: string[]] }>()
const tenant = useTenantStore()
const input = ref<HTMLInputElement>()
const uploading = ref(false)
async function select(event: Event): Promise<void> {
  const element = event.target as HTMLInputElement
  const files = Array.from(element.files ?? [])
  if (!files.length) return
  uploading.value = true
  try {
    const urls = props.multiple ? [...props.modelValue] : []
    for (const file of files) {
      if (file.size > 10 * 1024 * 1024) { ElMessage.warning('图片不能超过 10MB'); continue }
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) { ElMessage.warning('请选择 JPG、PNG 或 WebP 图片'); continue }
      const image = await catalogApi.uploadImage(tenant.requireTenantId(), file)
      urls.push(image.url)
    }
    emit('update:modelValue', props.multiple ? urls : urls.slice(-1))
  } finally { uploading.value = false; element.value = '' }
}
function remove(index: number): void { emit('update:modelValue', props.modelValue.filter((_, i) => i !== index)) }
function move(index: number, offset: number): void {
  const urls = [...props.modelValue]
  const next = index + offset
  if (next < 0 || next >= urls.length) return
  const value = urls[index]!
  urls[index] = urls[next]!
  urls[next] = value
  emit('update:modelValue', urls)
}
</script>
<template>
  <div class="catalog-images">
    <div v-for="(url, index) in modelValue" :key="`${index}-${url}`" class="catalog-image">
      <el-image :src="url" fit="cover" :preview-src-list="modelValue" preview-teleported />
      <div v-if="!disabled" class="image-actions">
        <el-button v-if="multiple" text size="small" :disabled="index === 0" @click="move(index, -1)">前移</el-button>
        <el-button v-if="multiple" text size="small" :disabled="index === modelValue.length - 1" @click="move(index, 1)">后移</el-button>
        <el-button text type="danger" size="small" @click="remove(index)">移除</el-button>
      </div>
    </div>
    <el-button v-if="!disabled" :loading="uploading" @click="input?.click()">{{ modelValue.length && !multiple ? '替换图片' : '上传图片' }}</el-button>
    <input ref="input" hidden type="file" accept="image/jpeg,image/png,image/webp" :multiple="multiple" @change="select" />
  </div>
</template>
<style scoped>
.catalog-images { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.catalog-image { width:120px; }
.catalog-image .el-image { width:120px; height:100px; border:1px solid var(--el-border-color); border-radius:5px; }
.image-actions { display:flex; justify-content:center; }
.image-actions .el-button + .el-button { margin-left:0; }
</style>
