import { afterEach, expect, it, vi } from 'vitest'
import { adminPluginsApi } from './adminPlugins'
import { handleErrorResponse, invalidateRequestGeneration, currentRequestGeneration, setPluginDisabledHandler } from './client'

afterEach(() => { setPluginDisabledHandler(null); vi.unstubAllGlobals() })
it('管理目录和启停发送预期版本、严格布尔值', async () => {
  const fetcher = vi.fn(async () => new Response('{}'))
  vi.stubGlobal('fetch', fetcher)
  await adminPluginsApi.list()
  await adminPluginsApi.setEnabled({ id: 'sample', name: '', description: '', version: '', enabled: false, revision: 3, updated_at: null }, true, {})
  expect(fetcher).toHaveBeenCalledWith('/api/v1/admin/plugins', expect.objectContaining({ method: 'GET' }))
  expect(fetcher).toHaveBeenCalledWith('/api/v1/admin/plugins/sample', expect.objectContaining({ method: 'PATCH', body: '{"revision":3,"enabled":true}' }))
})
it('仅当前会话的插件停用 403 通知刷新配置', async () => {
  const refresh = vi.fn()
  const disabled = () => new Response('{"code":"plugin_disabled"}', { status: 403 })
  setPluginDisabledHandler(refresh)
  const generation = currentRequestGeneration()
  await expect(handleErrorResponse('/plugins/sample', disabled())).rejects.toThrow()
  invalidateRequestGeneration()
  await expect(handleErrorResponse('/plugins/sample', disabled(), generation)).rejects.toThrow()
  expect(refresh).toHaveBeenCalledOnce()
})
