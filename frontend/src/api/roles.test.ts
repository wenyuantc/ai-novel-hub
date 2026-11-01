import { afterEach, describe, expect, it, vi } from 'vitest'
import { roleApi } from './roles'
import { ApiError, invalidateRequestGeneration, request, setPermissionDeniedHandler, setUnauthorizedHandler } from './client'
import { deferred } from '../components/systemSettings/systemSettingsTestFixtures'

afterEach(() => { vi.unstubAllGlobals(); setPermissionDeniedHandler(null); setUnauthorizedHandler(null) })

describe('角色分配与配额真实传输契约', () => {
  it('复制仍使用POST角色原语，CAS/空角色/三态零配额完整发送，不重发', async () => {
    const fetcher = vi.fn(async () => new Response('{}'))
    vi.stubGlobal('fetch', fetcher)
    const signal = new AbortController().signal
    await roleApi.permissions(signal)
    await roleApi.create({ name: '内容维护', source_role_id: 2, source_revision: 3, permissions: ['projects.edit'] })
    await roleApi.update(8, { revision: 1, authorization_revision: 12, permissions: [] })
    await roleApi.delete({ id: 8, revision: 2, authorization_revision: 13 })
    await roleApi.assign(9, { authorization_revision: 14, role_ids: [] })
    await roleApi.updateQuotas(9, { authorization_revision: 15, revision: 2, changes: { project_count: { mode: 'limited', value: 0 }, task_concurrency: { mode: 'unlimited' } } })
    const calls = fetcher.mock.calls as unknown as [string, RequestInit][]
    expect(calls.map(([url, init]) => [url, init.method])).toEqual([
      ['/api/v1/admin/permissions', 'GET'], ['/api/v1/admin/roles', 'POST'], ['/api/v1/admin/roles/8', 'PATCH'],
      ['/api/v1/admin/roles/8?revision=2&authorization_revision=13', 'DELETE'], ['/api/v1/admin/users/9/roles', 'PUT'], ['/api/v1/admin/users/9/quotas', 'PATCH'],
    ])
    expect(calls.every(([, init]) => init.credentials === 'include')).toBe(true)
    expect(calls[0][1].signal).toBe(signal)
    expect(JSON.parse(String(calls[4][1].body))).toEqual({ authorization_revision: 14, role_ids: [] })
    expect(JSON.parse(String(calls[5][1].body)).changes.project_count).toEqual({ mode: 'limited', value: 0 })
  })
  it('只有明确RBAC403请求权限刷新；保护目标403、409、me403不登出/不刷新/不重发', async () => {
    const denied = vi.fn(), unauthorized = vi.fn()
    setPermissionDeniedHandler(denied); setUnauthorizedHandler(unauthorized)
    const fetcher = vi.fn().mockResolvedValueOnce(new Response('{"code":"permission_denied"}', {status: 403}))
      .mockResolvedValueOnce(new Response('{"code":"management_account_protected"}', {status: 403}))
      .mockResolvedValueOnce(new Response('{"code":"stale_revision"}', {status: 409}))
      .mockResolvedValueOnce(new Response('{"code":"permission_denied"}', {status: 403}))
    vi.stubGlobal('fetch', fetcher)
    for (const path of ['/admin/roles', '/admin/users/2/quotas', '/admin/roles', '/auth/me']) await expect(request(path)).rejects.toBeInstanceOf(ApiError)
    expect(denied).toHaveBeenCalledOnce(); expect(unauthorized).not.toHaveBeenCalled(); expect(fetcher).toHaveBeenCalledTimes(4)
  })
  it.each([401, 403])('旧会话%s迟到不能清掉/刷新新账号', async (status) => {
    const slow = deferred<Response>(), denied = vi.fn(), unauthorized = vi.fn()
    setPermissionDeniedHandler(denied); setUnauthorizedHandler(unauthorized)
    vi.stubGlobal('fetch', vi.fn(() => slow.promise))
    const pending = request('/admin/roles')
    invalidateRequestGeneration()
    slow.resolve(new Response('{"code":"permission_denied"}', { status }))
    await expect(pending).rejects.toBeInstanceOf(ApiError)
    expect(denied).not.toHaveBeenCalled(); expect(unauthorized).not.toHaveBeenCalled()
  })
})
