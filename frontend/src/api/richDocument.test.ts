import { afterEach, describe, expect, it, vi } from 'vitest'
import { parseSectionRewriteDone, richDocumentApi, type SectionRewriteRequest } from './richDocument'
import type { Task } from './types'

afterEach(() => vi.unstubAllGlobals())

const requestBody: SectionRewriteRequest = {
  expected_content_revision: 0, expected_planning_revision: 2, request_id: '0981fe8e-f4cc-4336-80a2-df11c2812834',
  confirmed: true, op: 'rewrite', start: 4, end: 7, selected_hash: 'a'.repeat(64),
}
const context = { cid: 2, sid: 3, key: 'b3fe8e02-8fe9-45d1-a51e-128fdd211d01', body: requestBody, expectedOriginal: '选中🙂' }
const event = { text: '新稿', original: '选中🙂', before: '', start: 4, end: 7, log_id: 5, scene_id: 3, chapter_id: 2,
  content_revision: 0, planning_revision: 2, section_key: context.key, request_id: requestBody.request_id }

describe('富文档 API', () => {
  it('同一正文版本保存、转换和分页携带完整目标，不自动重试', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => new Response('{}'))
    vi.stubGlobal('fetch', fetch)
    await richDocumentApi.convert(1, 2, 3, context.key, 4)
    expect(fetch.mock.calls[0][0]).toBe(`/api/v1/projects/1/chapters/2/scenes/3/document/sections/${context.key}/convert`)
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({ expected_content_revision: 4 })
    await richDocumentApi.timeline(1, { offset: 20, limit: 20, snapshot: 'a'.repeat(64) })
    expect(fetch.mock.calls[1][0]).toContain('offset=20&limit=20&snapshot=')
  })

  it('只接受同一区块、范围、确认 UUID、原文与两个版本的真实结果', () => {
    expect(parseSectionRewriteDone(event, context)).toEqual(event)
    for (const altered of [
      { section_key: 'other' }, { request_id: 'other' }, { original: '相似文字' }, { content_revision: 1 },
      { planning_revision: 3 }, { scene_id: 4 }, { chapter_id: 3 }, { end: 8 }, { before: '额外前文' }, { log_id: null },
    ]) expect(() => parseSectionRewriteDone({ ...event, ...altered }, context)).toThrow()
  })

  it('提案采纳绑定三重身份并转发中止信号，不在中止后重发', async () => {
    const task = { id: 4, rich_review: { task_key: 'task-key', run_key: 'run-key', token: 'token', chapter_id: 2 } } as Task
    const fetch = vi.fn<typeof globalThis.fetch>(async (_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    }))
    vi.stubGlobal('fetch', fetch)
    const controller = new AbortController()
    const pending = richDocumentApi.acceptReview(task, [{ scene_id: 3, section_key: 'section', anchor: 1 }], true, controller.signal)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/tasks/4/rich-proposals/token/accept')
    expect(fetch.mock.calls[0][1]?.signal).toBe(controller.signal)
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({ task_key: 'task-key', run_key: 'run-key',
      sections: [{ scene_id: 3, section_key: 'section', anchor: 1 }], confirm_format_loss: true })
    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})
