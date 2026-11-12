import { http, requestPage } from './http'
import { segment, tenantPath } from './paths'
import type {
  EntityId,
  PageParams,
  PagedData,
  Product,
  ProductCreatePayload,
  ProductUpdatePayload,
  QueryParams,
} from '@/types'
import type { CatalogOptions, CatalogOption, SupplierOption, ProductLog, ProductSkuPayload } from '@/types/catalog'

export interface ProductQuery extends PageParams {
  q?: string
  status?: string
  sales_status?: string
  sales_type?: string
  brand?: string
  category?: string
  preferred_supplier_id?: string
  purchaser_id?: string
  developer_id?: string
  inquiry_buyer_id?: string
  tag?: string
}

export interface ProductQueryAll extends ProductQuery {
  page_size?: number
}

export const catalogApi = {
  options(tenantId: EntityId): Promise<CatalogOptions> {
    return http.get<CatalogOptions>(tenantPath(tenantId, '/products/options'))
  },
  createOption(tenantId: EntityId, payload: { kind: string; name: string; parent_id?: string }): Promise<CatalogOption> {
    return http.post<CatalogOption>(tenantPath(tenantId, '/products/options'), payload)
  },
  createSupplier(tenantId: EntityId, payload: { code: string; name: string }): Promise<SupplierOption> {
    return http.post<SupplierOption>(tenantPath(tenantId, '/products/suppliers'), payload)
  },
  uploadImage(tenantId: EntityId, file: File): Promise<{ url: string }> {
    const body = new FormData()
    body.append('file', file)
    return http.post<{ url: string }>(tenantPath(tenantId, '/products/media'), body)
  },
  logs(tenantId: EntityId, productId: EntityId, page = 1): Promise<PagedData<ProductLog>> {
    return requestPage<ProductLog>(tenantPath(tenantId, `/products/${segment(productId)}/logs`), { page, page_size: 20 })
  },
  createSku(tenantId: EntityId, productId: EntityId, payload: ProductSkuPayload): Promise<Product> {
    return http.post<Product>(tenantPath(tenantId, `/products/${segment(productId)}/skus`), payload)
  },
  updateSku(tenantId: EntityId, productId: EntityId, skuId: EntityId, payload: ProductSkuPayload): Promise<Product> {
    return http.patch<Product>(tenantPath(tenantId, `/products/${segment(productId)}/skus/${segment(skuId)}`), payload)
  },
  products(tenantId: EntityId, query: ProductQuery = {}): Promise<PagedData<Product>> {
    return requestPage<Product>(tenantPath(tenantId, '/products'), query as QueryParams)
  },

  async productOptions(tenantId: EntityId, q?: string): Promise<Product[]> {
    const page = await requestPage<Product>(tenantPath(tenantId, '/products'), {
      page: 1,
      page_size: 100,
      q,
      status: 'active',
    } as QueryParams)
    return page.items
  },

  product(tenantId: EntityId, productId: EntityId): Promise<Product> {
    return http.get<Product>(tenantPath(tenantId, `/products/${segment(productId)}`))
  },

  createProduct(tenantId: EntityId, payload: ProductCreatePayload): Promise<Product> {
    return http.post<Product>(tenantPath(tenantId, '/products'), payload)
  },

  updateProduct(tenantId: EntityId, productId: EntityId, payload: ProductUpdatePayload): Promise<Product> {
    return http.patch<Product>(tenantPath(tenantId, `/products/${segment(productId)}`), payload)
  },

  archiveProduct(tenantId: EntityId, productId: EntityId): Promise<Product> {
    return http.post<Product>(tenantPath(tenantId, `/products/${segment(productId)}/archive`))
  },
}
