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

export interface ProductQuery extends PageParams {
  q?: string
  status?: string
}

export interface ProductQueryAll extends ProductQuery {
  page_size?: number
}

export const catalogApi = {
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
