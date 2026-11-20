import { afterEach, describe, expect, it, vi } from 'vitest'
import { sceneMoveApi, sceneRestructureApi } from './sceneStructure'
import { canStreamTask, isChapterWriteKind, taskLink } from './tasks'
import type { SceneRestructureRequest } from './types'

const taskKey = '00000000-0000-4000-8000-000000000016'
const signature = 'a'.repeat(64)
const options = {
  signal: new AbortController().signal,
  isCurrent: () => true,
  editorActor: { id: 9, createdAt: '2026-11-20T09:30:00Z' },
}

afterEach(() => vi.unstubAllGlobals())

describe('场景结构请求合同', () => {
  it('双章确认带预览的位置和签名，两个动作都核对原编辑器身份', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => new Response('{}'))
    vi.stubGlobal('fetch', fetch)
    const placement = { target_chapter_id: 4, insert_before_scene_id: null }

    await sceneMoveApi.preview(1, 2, 3, placement, options)
    await sceneMoveApi.move(1, 2, 3, { ...placement, signature }, options)

    expect(fetch.mock.calls.map(([url, init]) => [url, init?.method, JSON.parse(String(init?.body))])).toEqual([
      ['/api/v1/projects/1/chapters/2/scenes/3/move-preview', 'POST', placement],
      ['/api/v1/projects/1/chapters/2/scenes/3/move', 'POST', { ...placement, signature }],
    ])
    for (const [, init] of fetch.mock.calls) {
      expect(init?.signal).toBe(options.signal)
      expect(init?.headers).toMatchObject({
        'X-Editor-Actor-Id': '9', 'X-Editor-Actor-Created-At': options.editorActor.createdAt,
      })
    }
  })

  it('AI 只发送已确认的范围与候选身份，重开和采纳使用任务 UUID', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => new Response('{}'))
    vi.stubGlobal('fetch', fetch)
    const operation: SceneRestructureRequest = { operation: 'merge', scene_ids: [3, 8], expected_content_revision: 7 }

    await sceneRestructureApi.preview(1, 2, operation, options)
    await sceneRestructureApi.start(1, 2, { ...operation, signature }, options)
    await sceneRestructureApi.result(1, 2, taskKey, options)
    await sceneRestructureApi.apply(1, 2, taskKey, { candidate_id: 1, signature, metadata_source: 'right' }, options)

    const route = '/api/v1/projects/1/chapters/2/scene-restructure'
    expect(fetch.mock.calls.map(([url, init]) => [url, init?.method, init?.body ? JSON.parse(String(init.body)) : undefined])).toEqual([
      [`${route}/preview`, 'POST', operation],
      [`${route}/suggestions`, 'POST', { ...operation, signature }],
      [`${route}/suggestions/${taskKey}`, 'GET', undefined],
      [`${route}/suggestions/${taskKey}/apply`, 'POST', { candidate_id: 1, signature, metadata_source: 'right' }],
    ])
  })

  it('建议不是章节写入任务，后台任务的查看入口不信任旧场景数字 ID', () => {
    const identity = { kind: 'scene.restructure' as const, project_id: 1, chapter_id: 2, scene_id: 3 }
    expect(isChapterWriteKind(identity.kind)).toBe(false)
    expect(canStreamTask(identity.kind, true)).toBe(true)
    expect(taskLink({ ...identity, task_key: taskKey })).toBe(`/projects/1/chapters/2?restructure_task=${taskKey}`)
    expect(taskLink({ ...identity, task_key: 'not-a-task-key' })).toBeNull()
    expect(taskLink({ ...identity, task_key: taskKey, chapter_id: null })).toBeNull()
  })
})
