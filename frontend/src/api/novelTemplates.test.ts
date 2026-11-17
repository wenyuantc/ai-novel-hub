import { afterEach, describe, expect, it, vi } from 'vitest'
import { setUnauthorizedHandler } from './client'
import { novelTemplatesApi, type NovelTemplateSelection } from './novelTemplates'

const selection: NovelTemplateSelection = { include_config: true, character_ids: [1], world_ids: [2],
  organization_ids: [], career_ids: [], outline_ids: [3] }

afterEach(() => { setUnauthorizedHandler(null); vi.unstubAllGlobals() })

describe('小说模板 API', () => {
  it('按独立接口提交选择、版本和稳定创建意图', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    await novelTemplatesApi.preview(10, selection)
    await novelTemplatesApi.create({ name: '模板', description: '', source_project_id: 10, selection, source_stamp: 'abc' })
    await novelTemplatesApi.replace(8, { source_project_id: 10, selection, source_stamp: 'def', expected_revision: 3 })
    await novelTemplatesApi.remove(8, 4)
    const intent = { expected_revision: 4, intent_key: 'a1234567-1111-4111-8111-123456789abc', title: '新书', pen_name_id: 2 }
    await novelTemplatesApi.createProject(8, intent)
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/projects/10/novel-template-preview', '/api/v1/novel-templates', '/api/v1/novel-templates/8/snapshot',
      '/api/v1/novel-templates/8?expected_revision=4', '/api/v1/novel-templates/8/projects',
    ])
    expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual(selection)
    expect(JSON.parse(String(fetcher.mock.calls[4][1]?.body))).toEqual(intent)
    expect(fetcher.mock.calls.every(([, init]) => init?.credentials === 'include')).toBe(true)
  })

  it('模板内容按分组分页；图片身份按路径编码并绑定版本', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    await novelTemplatesApi.get(8, undefined, { group: 'world_settings', page: 2, page_size: 20 })
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/novel-templates/8?group=world_settings&page=2&page_size=20')
    expect(novelTemplatesApi.imageUrl(8, 'world/2', 4)).toContain('/novel-templates/8/images/world%2F2?revision=4')
  })

  it('账号切换后迟到的模板请求失败不触发新会话登出', async () => {
    let reply!: (response: Response) => void
    let current = true
    const unauthorized = vi.fn()
    setUnauthorizedHandler(unauthorized)
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockImplementation(() => new Promise<Response>((resolve) => { reply = resolve })))
    const pending = novelTemplatesApi.list({ page: 1, page_size: 20 }, { isCurrent: () => current })
    current = false
    reply(new Response('{"detail":"已退出"}', { status: 401 }))
    await expect(pending).rejects.toMatchObject({ status: 401 })
    expect(unauthorized).not.toHaveBeenCalled()
  })
})
