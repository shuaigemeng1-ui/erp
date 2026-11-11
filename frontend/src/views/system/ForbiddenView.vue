<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()
</script>

<template>
  <el-result icon="warning" title="无访问权限" sub-title="当前账号缺少该页面所需权限，请联系管理员开通。">
    <template #extra>
      <el-button type="primary" @click="router.replace('/')">返回首页</el-button>
      <el-button @click="router.replace('/tenant-select')">切换租户</el-button>
    </template>
  </el-result>
</template>
