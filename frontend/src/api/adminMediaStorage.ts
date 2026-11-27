import { request, type RequestOptions } from './client'
import type { MediaConnectionTest, MediaStorageOverview, MediaStoragePatch } from './types'

const path = '/admin/media-storage'
type Scope = Pick<RequestOptions, 'isCurrent' | 'signal'>
export const adminMediaStorageApi = {
  get: (scope?: Scope) => request<MediaStorageOverview>(path, scope),
  update: (body: MediaStoragePatch, scope: Scope) =>
    request<MediaStorageOverview>(path, { ...scope, method: 'PATCH', body }),
  test: (body: MediaConnectionTest, scope: Scope) =>
    request<{ ok: boolean }>(path + '/test', { ...scope, method: 'POST', body }),
}
