import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, setUnauthorizedHandler } from './client'
import {
  COLLAB_AUDIT_ACTION,
  COLLAB_AUDIT_ERROR,
  COLLAB_AUDIT_RESULT,
  COLLAB_AUDIT_SCOPE,
  collaborationAuditApi,
} from './collaborationAudit'

function stubFetch(status = 200, body: unknown = { items: [], total: 0 }) {
  const calls: { url: string; init: RequestInit }[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(JSON.stringify(body), { status })
  })
  return calls
}

afterEach(() => {
  vi.unstubAllGlobals()
  setUnauthorizedHandler(null)
})

describe('协作审计 API', () => {
  it('作者接口按作品与系列作用域编码筛选参数，并忽略纯空白 member_q', async () => {
    const calls = stubFetch()
    const controller = new AbortController()
    await expect(
      collaborationAuditApi.listForOwner(
        'project',
        12,
        {
          member_user_id: 8,
          member_q: '  alice_%\\  ',
          action: 'modify',
          result: 'success',
          from_at: '2026-11-18T16:00:00.000Z',
          to_at: '2026-11-19T16:00:00.000Z',
          page: 2,
          page_size: 50,
        },
        controller.signal,
      ),
    ).resolves.toEqual({ items: [], total: 0 })
    const url = new URL(calls[0].url, 'http://test')
    expect(url.pathname).toBe('/api/v1/projects/12/collaborators/audit-logs')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      member_user_id: '8',
      member_q: 'alice_%\\',
      action: 'modify',
      result: 'success',
      from_at: '2026-11-18T16:00:00.000Z',
      to_at: '2026-11-19T16:00:00.000Z',
      page: '2',
      page_size: '50',
    })
    expect(calls[0].init.credentials).toBe('include')
    expect(calls[0].init.signal).toBe(controller.signal)

    await collaborationAuditApi.listForOwner('series', 5, { member_q: '   ' })
    expect(calls[1].url).toBe('/api/v1/series/5/collaborators/audit-logs')
  })

  it('管理员全站接口支持 scope、resource_id=0 及双侧用户筛选', async () => {
    const calls = stubFetch()
    await collaborationAuditApi.listForAdmin({
      scope: 'project',
      resource_id: 0,
      resource_q: '  旧作  ',
      actor_user_id: 3,
      actor_q: ' stranger ',
      target_user_id: 9,
      target_q: '  guest  ',
      action: 'accept',
      result: 'failure',
      page: 1,
      page_size: 20,
    })
    const url = new URL(calls[0].url, 'http://test')
    expect(url.pathname).toBe('/api/v1/admin/audit-logs/collaboration')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      scope: 'project',
      resource_id: '0',
      resource_q: '旧作',
      actor_user_id: '3',
      actor_q: 'stranger',
      target_user_id: '9',
      target_q: 'guest',
      action: 'accept',
      result: 'failure',
      page: '1',
      page_size: '20',
    })

    await collaborationAuditApi.listForAdmin({ resource_q: '  ', actor_q: '', target_q: ' ' })
    expect(calls[1].url).toBe('/api/v1/admin/audit-logs/collaboration')
  })

  it('标签映射覆盖全部作用域、动作、结果与 12 项白名单错误码', () => {
    expect(Object.keys(COLLAB_AUDIT_SCOPE)).toEqual(['project', 'series'])
    expect(Object.keys(COLLAB_AUDIT_ACTION)).toEqual(['invite', 'accept', 'decline', 'modify', 'revoke'])
    expect(Object.keys(COLLAB_AUDIT_RESULT)).toEqual(['success', 'failure'])
    expect(Object.keys(COLLAB_AUDIT_ERROR)).toHaveLength(12)
  })

  it('401 触发统一登出，403/404 保留安全错误而不登出', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    stubFetch(401, { detail: '未登录' })
    await expect(collaborationAuditApi.listForAdmin()).rejects.toBeInstanceOf(ApiError)
    expect(onUnauthorized).toHaveBeenCalledOnce()

    stubFetch(403, { detail: '没有权限', code: 'permission_denied', required_permission: 'audit.read' })
    await expect(collaborationAuditApi.listForAdmin()).rejects.toMatchObject({ status: 403 })
    stubFetch(404, { detail: '作品或系列不存在' })
    await expect(collaborationAuditApi.listForOwner('project', 99)).rejects.toMatchObject({ status: 404 })
    expect(onUnauthorized).toHaveBeenCalledOnce()
  })
})
