import { onMounted, ref, type Ref } from 'vue'
import { isAbortError } from '@/api/http'
import type { PagedData, PageParams } from '@/types'

export interface UseTableQueryOptions {
  pageSize?: number
  immediate?: boolean
}

export interface TableQuery<T> {
  rows: Ref<T[]>
  total: Ref<number>
  page: Ref<number>
  pageSize: Ref<number>
  loading: Ref<boolean>
  error: Ref<string>
  load: () => Promise<void>
  search: () => void
  reload: () => Promise<void>
  handlePageChange: (page: number) => void
  handleSizeChange: (size: number) => void
}

/**
 * 统一列表加载：请求序号防止迟到响应覆盖新数据，abort（租户切换）不报错。
 * fetcher 必须显式使用页面传入的 tenant_id。
 */
export function useTableQuery<T>(
  fetcher: (params: PageParams) => Promise<PagedData<T>>,
  options: UseTableQueryOptions = {},
): TableQuery<T> {
  const rows = ref([]) as Ref<T[]>
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(options.pageSize ?? 20)
  const loading = ref(false)
  const error = ref('')
  let requestSeq = 0

  async function load(): Promise<void> {
    const seq = ++requestSeq
    loading.value = true
    error.value = ''
    try {
      const result = await fetcher({ page: page.value, page_size: pageSize.value })
      if (seq !== requestSeq) return
      rows.value = result.items
      total.value = result.total
    } catch (cause) {
      if (seq !== requestSeq) return
      if (isAbortError(cause)) return
      rows.value = []
      total.value = 0
      error.value = cause instanceof Error ? cause.message : '加载失败'
    } finally {
      if (seq === requestSeq) loading.value = false
    }
  }

  function search(): void {
    page.value = 1
    void load()
  }

  function handlePageChange(nextPage: number): void {
    page.value = nextPage
    void load()
  }

  function handleSizeChange(nextSize: number): void {
    pageSize.value = nextSize
    page.value = 1
    void load()
  }

  if (options.immediate !== false) onMounted(() => void load())

  return {
    rows,
    total,
    page,
    pageSize,
    loading,
    error,
    load,
    search,
    reload: load,
    handlePageChange,
    handleSizeChange,
  }
}
