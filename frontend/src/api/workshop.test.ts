import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { workshopApi } from './workshop'
import { emptyWorkshopShare } from '../utils/workshop'

afterEach(() => vi.unstubAllGlobals())
function stub(status = 200, body: unknown = {}) {
  const calls: { url: string; init: RequestInit }[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(status === 204 ? null : JSON.stringify(body), { status })
  })
  return calls
}
describe('工坊接口契约', () => {
  it('筛选分页、本人列表与审核列表使用各自路径，身份由 cookie 提供', async () => {
    const calls = stub()
    await workshopApi.list({ kind: 'prompt', category: 'suspense', tag: '反转', prompt_key: 'chapter.generate', q: '雨 夜', sort: 'likes', page: 2, page_size: 24 })
    await workshopApi.mine(3)
    await workshopApi.submissions({ status: 'rejected', page: 2, page_size: 12 })
    await workshopApi.pending({ status: 'pending', page: 1, page_size: 1 })
    const query = new URL(calls[0].url, 'http://localhost').searchParams
    expect(Object.fromEntries(query)).toEqual({ kind: 'prompt', category: 'suspense', tag: '反转', prompt_key: 'chapter.generate', q: '雨 夜', sort: 'likes', page: '2', page_size: '24' })
    expect(calls.slice(1).map((c) => c.url)).toEqual([
      '/api/v1/workshop/mine/items?page=3&page_size=12', '/api/v1/workshop/submissions?status=rejected&page=2&page_size=12',
      '/api/v1/workshop/admin/submissions?status=pending&page=1&page_size=1',
    ])
    expect(calls.every((c) => c.init.credentials === 'include')).toBe(true)
  })
  it('发布、审核、撤回、元数据、点赞和导入的 HTTP 方法及请求体', async () => {
    const calls = stub(200, { id: 7, liked: false, like_count: 2 })
    const body = { ...emptyWorkshopShare('style'), name: '风格' }
    await workshopApi.submit(body)
    await workshopApi.publish(body)
    await workshopApi.get(7)
    await workshopApi.submission(3)
    await workshopApi.withdraw(3)
    await workshopApi.approve(3, { category: 'scifi', tags: ['星际'] })
    await workshopApi.reject(3, '请补充说明')
    await workshopApi.hide(7)
    await workshopApi.unhide(7)
    await workshopApi.like(7)
    expect(await workshopApi.unlike(7)).toMatchObject({ liked: false, like_count: 2 })
    await workshopApi.import(7)
    await workshopApi.metadata(7, { tags: [] })
    await workshopApi.remove(7)
    expect(calls.map((c) => [c.init.method, c.url.replace('/api/v1/workshop', '')])).toEqual([
      ['POST', '/submissions'], ['POST', '/admin/items'], ['GET', '/items/7'], ['GET', '/submissions/3'],
      ['POST', '/submissions/3/withdraw'], ['POST', '/admin/submissions/3/approve'], ['POST', '/admin/submissions/3/reject'],
      ['POST', '/items/7/hide'], ['POST', '/items/7/unhide'], ['POST', '/items/7/like'], ['DELETE', '/items/7/like'],
      ['POST', '/items/7/import'], ['PATCH', '/admin/items/7'], ['DELETE', '/admin/items/7'],
    ])
    expect(JSON.parse(String(calls[0].init.body))).toEqual(body)
    expect(JSON.parse(String(calls[5].init.body))).toEqual({ category: 'scifi', tags: ['星际'] })
    expect(JSON.parse(String(calls[6].init.body))).toEqual({ note: '请补充说明' })
    expect(JSON.parse(String(calls[12].init.body))).toEqual({ tags: [] })
  })
  it('导入 400 保留逐行模板错误，409 重复 pending 保留服务端说明', async () => {
    const errors = [{ part: 'user', line: 2, message: '缺少锁定变量' }]
    stub(400, { detail: '模板校验失败', errors })
    const err = await workshopApi.import(2).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).body?.errors).toEqual(errors)
    stub(409, { detail: '该条目已有待审核更新' })
    await expect(workshopApi.submit({ ...emptyWorkshopShare('style'), item_id: 2 })).rejects.toMatchObject({ status: 409, detail: '该条目已有待审核更新' })
  })
})
