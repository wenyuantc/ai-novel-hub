import { afterEach, expect, it, vi } from 'vitest'
import { chapterApi } from './chapters'
import { richDocumentApi } from './richDocument'
import { sceneApi } from './scenes'

afterEach(() => vi.unstubAllGlobals())

it('保存 API 绑定原编辑账号代次，章标题走独立的四字段条件写入口', async () => {
  const request = vi.fn<(url: string, options: RequestInit) => Promise<Response>>(async () => Response.json({}))
  vi.stubGlobal('fetch', request)
  const actor = { id: 7, createdAt: '2026-11-17T12:00:00.123456Z' }
  const guard = () => true
  const title = { title: '新标题', expected_title: '', expected_scene_id: 5, expected_content_revision: 4 }
  await chapterApi.saveTitle(2, 3, title, actor, guard)
  await sceneApi.update(2, 3, 5, { summary: '摘要', expected_content_revision: 4 }, guard, actor)
  await richDocumentApi.save(2, 3, 5, {
    expected_content_revision: 4, document: { content: '新正文', formatting: { version: 1, boundaries: [], inline: [], blocks: [] },
      sections: [], markers: [], retired_section_keys: [] },
    add_section_keys: [], delete_section_keys: [], add_marker_keys: [], delete_marker_keys: [],
    mapped_section_keys: [], mapped_marker_keys: [],
  }, undefined, guard, actor)
  expect(request.mock.calls.map(([url]) => url)).toEqual([
    '/api/v1/projects/2/chapters/3/editor-title', '/api/v1/projects/2/chapters/3/scenes/5', '/api/v1/projects/2/chapters/3/scenes/5/document',
  ])
  for (const [, options] of request.mock.calls) {
    const headers = new Headers(options.headers)
    expect(headers.get('X-Editor-Actor-Id')).toBe('7')
    expect(headers.get('X-Editor-Actor-Created-At')).toBe(actor.createdAt)
    expect(options.credentials).toBe('include')
  }
  expect(JSON.parse(String(request.mock.calls[0][1].body))).toEqual(title)
})
