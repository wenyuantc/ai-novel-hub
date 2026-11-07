import { afterEach, describe, expect, it, vi } from 'vitest'
import { parseSceneRewriteDone, readChapterScenes, sceneApi } from './scenes'
import { versionApi } from './chapterVersions'
import { taskLink } from './tasks'

afterEach(() => vi.unstubAllGlobals())
describe('场景 API 合同', () => {
  it('统一 cookie、路径、码点参数并保留 DELETE 版本响应', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => new Response(JSON.stringify({ items: [], content_revision: 4, planning_revision: 2 })))
    vi.stubGlobal('fetch', fetch)
    await sceneApi.split(1, 2, 3, { expected_content_revision: 3, offset: 2 })
    expect(fetch.mock.calls[0]).toEqual(['/api/v1/projects/1/chapters/2/scenes/3/split', expect.objectContaining({ method: 'POST', credentials: 'include', body: JSON.stringify({ expected_content_revision: 3, offset: 2 }) })])
    expect(await sceneApi.remove(1, 2, 3, 3)).toEqual({ items: [], content_revision: 4, planning_revision: 2 })
    expect(fetch.mock.calls[1][0]).toBe('/api/v1/projects/1/chapters/2/scenes/3?expected_content_revision=3')
  })
  it('位置解析只传服务端指纹和码点，不自己拼接正文', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => new Response('{}'))
    vi.stubGlobal('fetch', fetch)
    await sceneApi.resolvePosition(1, 2, 'abc', 3, 5)
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/projects/1/chapters/2/scenes/resolve-position?fingerprint=abc&start=3&end=5')
  })
  it('目录分页中版本变化拒绝排序使用，不能把当前页冒充完整列表', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ id: 1 }], total: 2, content_revision: 1, planning_revision: 1 })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ id: 2 }], total: 2, content_revision: 2, planning_revision: 1 })))
    vi.stubGlobal('fetch', fetch)
    await expect(readChapterScenes(1, 2)).rejects.toMatchObject({ status: 409 })
    expect(fetch.mock.calls[1][0]).toContain('offset=1&limit=100')
  })
  it('场景AI和旧稿提取有明确目标与版本，任务深链不丢场景', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => new Response('{}'))
    vi.stubGlobal('fetch', fetch)
    await sceneApi.generate(1, 2, 3, { target_words: 500, expected_content_revision: 4, expected_planning_revision: 5 })
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/projects/1/chapters/2/scenes/3/generate')
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({ target_words: 500, expected_content_revision: 4, expected_planning_revision: 5 })
    await versionApi.extract(1, 2, 6, { scene_id: 3, expected_content_revision: 4, signature: 'hash' })
    expect(fetch.mock.calls[1][0]).toBe('/api/v1/projects/1/chapters/2/versions/6/extract')
    expect(taskLink({ kind: 'chapter.generate', project_id: 1, chapter_id: 2, scene_id: 3 })).toBe('/projects/1/chapters/2?scene_id=3')
    expect(taskLink({ kind: 'chapter.revise', project_id: 1, chapter_id: 2, scene_id: 3 })).toBe('/projects/1/chapters/2?panel=analysis&scene_id=3')
  })
  it('局部SSE缺少目标或快照不能采纳，版本0合法', () => {
    const value = { text: '新稿😀', original: '旧稿', before: '', start: 0, end: 2, log_id: null, scene_id: 3, chapter_id: 2, content_revision: 0, planning_revision: 0 }
    expect(parseSceneRewriteDone(value)).toEqual(value)
    expect(() => parseSceneRewriteDone({ ...value, scene_id: null })).toThrow()
    expect(() => parseSceneRewriteDone({ ...value, content_revision: undefined })).toThrow()
    expect(() => parseSceneRewriteDone({ ...value, end: -1 })).toThrow()
  })
})
