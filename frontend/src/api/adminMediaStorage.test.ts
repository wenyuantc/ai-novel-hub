import { afterEach, expect, it, vi } from 'vitest'
import { adminMediaStorageApi } from './adminMediaStorage'

afterEach(() => vi.unstubAllGlobals())

it('管理配置的读、改、测试和历史位置检测分别调用对应接口', async () => {
  const fetcher = vi.fn(async () => new Response('{}'))
  vi.stubGlobal('fetch', fetcher)
  const scope = { isCurrent: () => true }
  await adminMediaStorageApi.get()
  await adminMediaStorageApi.update({ revision: 4, default_quota_bytes: 1024 }, scope)
  await adminMediaStorageApi.test({ kind: 'local' }, scope)
  await adminMediaStorageApi.test({ location_id: 2 }, scope)
  const path = '/api/v1/admin/media-storage'
  expect(fetcher).toHaveBeenNthCalledWith(1, path, expect.objectContaining({ method: 'GET', credentials: 'include' }))
  expect(fetcher).toHaveBeenNthCalledWith(2, path, expect.objectContaining({ method: 'PATCH', body: '{"revision":4,"default_quota_bytes":1024}' }))
  expect(fetcher).toHaveBeenNthCalledWith(3, path + '/test', expect.objectContaining({ method: 'POST', body: '{"kind":"local"}' }))
  expect(fetcher).toHaveBeenNthCalledWith(4, path + '/test', expect.objectContaining({ method: 'POST', body: '{"location_id":2}' }))
})
