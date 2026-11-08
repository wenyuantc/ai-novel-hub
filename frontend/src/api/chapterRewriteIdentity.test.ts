import { afterEach, describe, expect, it, vi } from 'vitest'
import { applyRewrite, rewriteChapter } from './chapterVersions'

const done = { text: '新', start: 1, end: 2, original: '甲', before: '前', log_id: 4,
  scene_id: 3, chapter_id: 2, content_revision: 7, planning_revision: 9 }
const response = (value: unknown) => new Response(`event: done\ndata: ${JSON.stringify(value)}\n\n`, {
  headers: { 'content-type': 'text/event-stream' },
})

afterEach(() => vi.unstubAllGlobals())

describe('章级选区改写版本绑定', () => {
  it('读取并验证预览的章、场景和双版本', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response(done)))
    await expect(rewriteChapter(2, { op: 'rewrite', start: 1, end: 2 }, () => {})).resolves.toEqual(done)
  })

  it('缺少版本的 done 不进入可采纳预览', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response({ ...done, content_revision: undefined })))
    await expect(rewriteChapter(2, { op: 'rewrite', start: 1, end: 2 }, () => {})).rejects.toThrow('缺少目标或版本')
  })

  it('显式确认和双版本原样提交，不自动重试冲突', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{"detail":"已变化","code":"scene_context_changed"}', { status: 409 }))
    vi.stubGlobal('fetch', fetcher)
    const body = { start: 1, end: 2, original: '甲', before: '前', replacement: '新', log_id: 4,
      expected_content_revision: 7, expected_planning_revision: 9, confirm_format_loss: true }
    await expect(applyRewrite(1, 2, body)).rejects.toMatchObject({ status: 409 })
    expect(fetcher).toHaveBeenCalledOnce()
    expect(JSON.parse(String(fetcher.mock.calls[0][1].body))).toEqual(body)
  })
})
