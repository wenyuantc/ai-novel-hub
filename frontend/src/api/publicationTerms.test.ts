import { afterEach, describe, expect, it, vi } from 'vitest'
import { publicationTermsApi } from './publicationTerms'
import { setUnauthorizedHandler } from './client'

afterEach(() => { vi.unstubAllGlobals(); setUnauthorizedHandler(null) })

describe('发布词表的账号与版本隔离', () => {
  it.each([['personal', '/api/v1/publication-terms'], ['global', '/api/v1/admin/publication-terms']] as const)(
    '%s 只读写对应词表，携带原账号与读取时的词表修订号', async (scope, path) => {
      const fetch = vi.fn(async (_url: string, init: RequestInit) => Response.json({
        terms: init.method === 'PUT' ? ['🎉词'] : [], revision: init.method === 'PUT' ? 1 : 0,
      }))
      vi.stubGlobal('fetch', fetch)
      const identity = { editorActor: { id: 17, createdAt: '2026-11-23T08:00:00Z' } }
      const snapshot = await publicationTermsApi.get(scope, identity)
      expect(await publicationTermsApi.update(scope, ['🎉词'], snapshot.revision, identity)).toEqual({ terms: ['🎉词'], revision: 1 })
      expect(fetch.mock.calls.map(([url]) => url)).toEqual([path, path])
      const calls = fetch.mock.calls.map(([, init]) => init)
      expect(calls.map((init) => init.method)).toEqual(['GET', 'PUT'])
      for (const call of calls) expect(call.headers).toMatchObject({
        'X-Editor-Actor-Id': '17', 'X-Editor-Actor-Created-At': '2026-11-23T08:00:00Z',
      })
      expect(JSON.parse(String(calls[1].body))).toEqual({ terms: ['🎉词'], expected_revision: 0 })
    })
  it('409 保留为冲突，不自动重发旧整表', async () => {
    const fetch = vi.fn(async () => new Response('{"detail":"词表已修改"}', { status: 409 }))
    vi.stubGlobal('fetch', fetch)
    await expect(publicationTermsApi.update('global', ['甲'], 3, {})).rejects.toMatchObject({ status: 409 })
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it('前账号的请求返回401时，当前账号保留', async () => {
    const clear = vi.fn()
    setUnauthorizedHandler(clear)
    vi.stubGlobal('fetch', async () => new Response('{}', { status: 401 }))
    await expect(publicationTermsApi.get('personal', { isCurrent: () => false })).rejects.toMatchObject({ status: 401 })
    expect(clear).not.toHaveBeenCalled()
  })
})
