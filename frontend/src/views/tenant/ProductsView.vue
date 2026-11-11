<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { catalogApi } from '@/api/catalog'
import { useTableQuery } from '@/composables/useTableQuery'
import { useTenantStore } from '@/stores/tenant'
import PaginationBar from '@/components/PaginationBar.vue'
import StatusTag from '@/components/StatusTag.vue'
import IdCopy from '@/components/IdCopy.vue'
import { formatDateTime, formatDecimal, formatMoney, parseJsonObject, stringOrUndefined, textOrDash } from '@/utils/format'
import type { Product, ProductCreatePayload, ProductSkuPayload } from '@/types'

const tenant = useTenantStore()
const tid = (): string => tenant.requireTenantId()

const canCreate = computed(() => tenant.has('catalog.product.create'))
const canUpdate = computed(() => tenant.has('catalog.product.update'))
const canReadCost = computed(() => tenant.has('catalog.cost.read'))

const query = reactive({
  q: '',
  status: '',
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
} = useTableQuery<Product>((params) =>
  catalogApi.products(tid(), {
    page: params.page,
    page_size: params.page_size,
    q: stringOrUndefined(query.q),
    status: stringOrUndefined(query.status),
  }),
)

/* ------------------------------ 新建 / 编辑 ------------------------------ */
interface SkuRow {
  sku_code: string
  name: string
  price: number | undefined
  cost_amount: number | undefined
  specs_text: string
}

const dialogVisible = ref(false)
const submitting = ref(false)
const editingProduct = ref<Product | null>(null)
const formRef = ref<FormInstance>()
const form = reactive({
  code: '',
  name: '',
  brand: '',
  category: '',
  base_price: undefined as number | undefined,
  cost_amount: undefined as number | undefined,
  description: '',
  images_text: '',
  skus: [] as SkuRow[],
})
const rules: FormRules = {
  code: [{ required: true, message: '请输入商品编码', trigger: 'blur' }],
  name: [{ required: true, message: '请输入商品名称', trigger: 'blur' }],
  base_price: [{ required: true, message: '请输入基础售价', trigger: 'blur' }],
}

function resetForm(): void {
  form.code = ''
  form.name = ''
  form.brand = ''
  form.category = ''
  form.base_price = undefined
  form.cost_amount = undefined
  form.description = ''
  form.images_text = ''
  form.skus = []
}

function addSku(): void {
  form.skus.push({ sku_code: '', name: '', price: undefined, cost_amount: undefined, specs_text: '' })
}

function removeSku(index: number): void {
  form.skus.splice(index, 1)
}

function openCreate(): void {
  editingProduct.value = null
  resetForm()
  addSku()
  dialogVisible.value = true
}

function openEdit(product: Product): void {
  editingProduct.value = product
  form.code = product.code
  form.name = product.name
  form.brand = product.brand ?? ''
  form.category = product.category ?? ''
  form.base_price = typeof product.base_price === 'number' ? product.base_price : Number(product.base_price) || undefined
  form.cost_amount =
    product.cost_amount === null || product.cost_amount === undefined
      ? undefined
      : typeof product.cost_amount === 'number'
        ? product.cost_amount
        : Number(product.cost_amount)
  form.description = product.description ?? ''
  form.images_text = (product.images ?? []).map((image) => image.url).join('\n')
  form.skus = []
  dialogVisible.value = true
}

async function submit(): Promise<void> {
  const formInstance = formRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return

  if (editingProduct.value) {
    submitting.value = true
    try {
      await catalogApi.updateProduct(tid(), editingProduct.value.id, {
        code: form.code.trim(),
        name: form.name.trim(),
        brand: form.brand.trim() || undefined,
        category: form.category.trim() || undefined,
        base_price: form.base_price,
        ...(canReadCost.value && form.cost_amount !== undefined ? { cost_amount: form.cost_amount } : {}),
        description: form.description.trim() || undefined,
      })
      ElMessage.success('商品已更新')
      dialogVisible.value = false
      await reload()
    } finally {
      submitting.value = false
    }
    return
  }

  const skuPayloads: ProductSkuPayload[] = []
  for (const [index, sku] of form.skus.entries()) {
    if (!sku.sku_code.trim() || !sku.name.trim() || sku.price === undefined) {
      ElMessage.warning(`第 ${index + 1} 行 SKU 需要填写编码、名称和价格`)
      return
    }
    let specs: Record<string, string> | undefined
    if (sku.specs_text.trim()) {
      const parsed = parseJsonObject(sku.specs_text)
      if (!parsed) {
        ElMessage.warning(`第 ${index + 1} 行 SKU 规格不是合法 JSON 对象`)
        return
      }
      specs = Object.fromEntries(Object.entries(parsed).map(([key, value]) => [key, String(value)]))
    }
    skuPayloads.push({
      sku_code: sku.sku_code.trim(),
      name: sku.name.trim(),
      price: sku.price,
      ...(canReadCost.value && sku.cost_amount !== undefined ? { cost_amount: sku.cost_amount } : {}),
      ...(specs ? { specs } : {}),
    })
  }
  if (skuPayloads.length === 0) {
    ElMessage.warning('请至少添加一个 SKU')
    return
  }
  if (form.base_price === undefined) {
    ElMessage.warning('请输入基础售价')
    return
  }

  const payload: ProductCreatePayload = {
    code: form.code.trim(),
    name: form.name.trim(),
    brand: form.brand.trim() || undefined,
    category: form.category.trim() || undefined,
    base_price: form.base_price,
    ...(canReadCost.value && form.cost_amount !== undefined ? { cost_amount: form.cost_amount } : {}),
    description: form.description.trim() || undefined,
    skus: skuPayloads,
    images: form.images_text
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line.length > 0),
  }

  submitting.value = true
  try {
    await catalogApi.createProduct(tid(), payload)
    ElMessage.success('商品已创建')
    dialogVisible.value = false
    await reload()
  } finally {
    submitting.value = false
  }
}

/* ------------------------------ 详情 / 归档 ------------------------------ */
const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref<Product | null>(null)

async function openDetail(product: Product): Promise<void> {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await catalogApi.product(tid(), product.id)
  } finally {
    detailLoading.value = false
  }
}

async function archive(product: Product): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认归档商品「${product.name}」？归档后不再出现在默认列表中。`, '归档商品', {
      type: 'warning',
      confirmButtonText: '确认归档',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await catalogApi.archiveProduct(tid(), product.id)
  ElMessage.success('商品已归档')
  await reload()
}

function specsText(specs: Record<string, string> | null | undefined): string {
  if (!specs || Object.keys(specs).length === 0) return '-'
  return Object.entries(specs)
    .map(([key, value]) => `${key}:${value}`)
    .join(', ')
}
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="filter-bar">
      <el-input v-model="query.q" placeholder="商品名称/编码" clearable style="width: 220px" @keyup.enter="search()" />
      <el-select v-model="query.status" placeholder="状态" clearable style="width: 140px" @change="search()">
        <el-option label="正常" value="active" />
        <el-option label="已归档" value="archived" />
      </el-select>
      <el-button type="primary" :icon="Search" @click="search()">查询</el-button>
      <el-button :icon="Refresh" @click="reload()">刷新</el-button>
      <div style="flex: 1" />
      <el-button v-if="canCreate" type="primary" :icon="Plus" @click="openCreate">新建商品</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" border size="small" row-key="id">
      <el-table-column label="ID" width="100">
        <template #default="{ row }"><IdCopy :id="row.id" /></template>
      </el-table-column>
      <el-table-column prop="code" label="编码" width="150" />
      <el-table-column prop="name" label="名称" min-width="180" show-overflow-tooltip />
      <el-table-column label="品牌/分类" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">{{ textOrDash(row.brand) }} / {{ textOrDash(row.category) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }"><StatusTag :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="基础售价" width="120" align="right">
        <template #default="{ row }">{{ formatDecimal(row.base_price) }}</template>
      </el-table-column>
      <el-table-column v-if="canReadCost" label="成本" width="120" align="right">
        <template #default="{ row }">{{ formatMoney(row.cost_amount, row.cost_currency) }}</template>
      </el-table-column>
      <el-table-column label="SKU" width="80" align="center">
        <template #default="{ row }">{{ row.skus?.length ?? '-' }}</template>
      </el-table-column>
      <el-table-column label="映射" width="120">
        <template #default="{ row }">
          <span v-if="row.mapping_summary">
            已映射 {{ row.mapping_summary.mapped ?? 0 }} / 待处理 {{ row.mapping_summary.pending ?? 0 }}
          </span>
          <span v-else class="text-muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="190" fixed="right">
        <template #default="{ row }">
          <el-button text type="primary" size="small" @click="openDetail(row as Product)">详情</el-button>
          <el-button v-if="canUpdate" text type="primary" size="small" @click="openEdit(row as Product)">编辑</el-button>
          <el-button
            v-if="canUpdate && row.status !== 'archived'"
            text
            type="danger"
            size="small"
            @click="archive(row as Product)"
          >
            归档
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <PaginationBar
      :total="total"
      :page="page"
      :page-size="pageSize"
      @page-change="handlePageChange"
      @size-change="handleSizeChange"
    />
  </el-card>

  <!-- 新建 / 编辑 -->
  <el-dialog
    v-model="dialogVisible"
    :title="editingProduct ? '编辑商品' : '新建商品'"
    width="820px"
    destroy-on-close
    top="6vh"
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-width="96px">
      <el-row :gutter="12">
        <el-col :span="12">
          <el-form-item label="商品编码" prop="code">
            <el-input v-model="form.code" placeholder="如 SKU-001" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="商品名称" prop="name">
            <el-input v-model="form.name" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="品牌">
            <el-input v-model="form.brand" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="分类">
            <el-input v-model="form.category" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="基础售价" prop="base_price">
            <el-input-number v-model="form.base_price" :min="0" :precision="2" :controls="false" style="width: 100%" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item v-if="canReadCost" label="成本">
            <el-input-number v-model="form.cost_amount" :min="0" :precision="2" :controls="false" style="width: 100%" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item label="描述">
        <el-input v-model="form.description" type="textarea" :rows="2" />
      </el-form-item>

      <template v-if="!editingProduct">
        <el-form-item label="SKU">
          <div style="width: 100%">
            <el-table :data="form.skus" border size="small">
              <el-table-column label="SKU 编码" min-width="130">
                <template #default="{ row }"><el-input v-model="row.sku_code" placeholder="必填" /></template>
              </el-table-column>
              <el-table-column label="名称" min-width="130">
                <template #default="{ row }"><el-input v-model="row.name" placeholder="必填" /></template>
              </el-table-column>
              <el-table-column label="售价" width="130">
                <template #default="{ row }">
                  <el-input-number v-model="row.price" :min="0" :precision="2" :controls="false" style="width: 100%" />
                </template>
              </el-table-column>
              <el-table-column v-if="canReadCost" label="成本" width="130">
                <template #default="{ row }">
                  <el-input-number v-model="row.cost_amount" :min="0" :precision="2" :controls="false" style="width: 100%" />
                </template>
              </el-table-column>
              <el-table-column label="规格 JSON" min-width="150">
                <template #default="{ row }"><el-input v-model="row.specs_text" placeholder='{"颜色":"红"}' /></template>
              </el-table-column>
              <el-table-column label="" width="70" align="center">
                <template #default="{ $index }">
                  <el-button text type="danger" size="small" @click="removeSku($index)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
            <el-button text type="primary" :icon="Plus" style="margin-top: 6px" @click="addSku">添加 SKU</el-button>
          </div>
        </el-form-item>
      </template>
      <el-form-item v-else label="SKU">
        <span class="form-tip">SKU 在新建时写入；如需调整请归档后重建。</span>
      </el-form-item>

      <el-form-item v-if="!editingProduct" label="图片地址">
        <el-input v-model="form.images_text" type="textarea" :rows="2" placeholder="每行一个图片 URL" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
    </template>
  </el-dialog>

  <!-- 详情 -->
  <el-drawer v-model="detailVisible" title="商品详情" size="720px" destroy-on-close>
    <el-skeleton v-if="detailLoading" :rows="6" animated />
    <template v-else-if="detail">
      <el-descriptions :column="2" border size="small" class="detail-descriptions">
        <el-descriptions-item label="ID"><IdCopy :id="detail.id" /></el-descriptions-item>
        <el-descriptions-item label="编码">{{ detail.code }}</el-descriptions-item>
        <el-descriptions-item label="名称">{{ detail.name }}</el-descriptions-item>
        <el-descriptions-item label="状态"><StatusTag :status="detail.status" /></el-descriptions-item>
        <el-descriptions-item label="品牌">{{ textOrDash(detail.brand) }}</el-descriptions-item>
        <el-descriptions-item label="分类">{{ textOrDash(detail.category) }}</el-descriptions-item>
        <el-descriptions-item label="基础售价">{{ formatDecimal(detail.base_price) }}</el-descriptions-item>
        <el-descriptions-item v-if="canReadCost" label="成本">
          {{ formatMoney(detail.cost_amount, detail.cost_currency) }}
        </el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ formatDateTime(detail.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="更新时间">{{ formatDateTime(detail.updated_at) }}</el-descriptions-item>
      </el-descriptions>

      <div style="font-weight: 600; margin: 12px 0 8px">SKU 列表</div>
      <el-table :data="detail.skus ?? []" border size="small" empty-text="暂无 SKU">
        <el-table-column label="SKU ID" width="110">
          <template #default="{ row }"><IdCopy :id="row.id" /></template>
        </el-table-column>
        <el-table-column prop="sku_code" label="SKU 编码" width="140" />
        <el-table-column prop="name" label="名称" min-width="120" />
        <el-table-column label="规格" min-width="150">
          <template #default="{ row }">{{ specsText(row.specs) }}</template>
        </el-table-column>
        <el-table-column label="售价" width="110" align="right">
          <template #default="{ row }">{{ formatDecimal(row.price) }}</template>
        </el-table-column>
        <el-table-column v-if="canReadCost" label="成本" width="110" align="right">
          <template #default="{ row }">{{ formatDecimal(row.cost_amount) }}</template>
        </el-table-column>
      </el-table>

      <div v-if="detail.images && detail.images.length" style="margin-top: 12px">
        <div style="font-weight: 600; margin-bottom: 8px">图片</div>
        <div v-for="image in detail.images" :key="image.url" class="mono">{{ image.url }}</div>
      </div>
    </template>
  </el-drawer>
</template>
