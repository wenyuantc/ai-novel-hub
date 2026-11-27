import { afterEach, expect, it, vi } from 'vitest'
import { videoProbeApi } from './videoProbe'

afterEach(() => vi.unstubAllGlobals())

it('读取上次测试，用个人会话上传参考图、按对象编号启动任务', async () => {
  const fetch = vi.fn<(url: string, init: RequestInit) => Promise<Response>>()
    .mockResolvedValueOnce(Response.json(null))
    .mockResolvedValueOnce(Response.json({ object_id: 'image-uuid' }, { status: 201 }))
    .mockResolvedValueOnce(Response.json({ id: 9, status: 'queued' }, { status: 202 }))
  vi.stubGlobal('fetch', fetch)
  const scope = { signal: new AbortController().signal, editorActor: { id: 4, createdAt: '2026-01-01' } }
  expect(await videoProbeApi.latest(3, scope)).toBeNull()
  const reference = await videoProbeApi.uploadReference(3, new File(['img'], '角色.png', { type: 'image/png' }), scope)
  expect(await videoProbeApi.start(3, [reference.object_id], scope)).toMatchObject({ id: 9 })
  expect(fetch.mock.calls.map(([url]) => url)).toEqual([
    '/api/v1/ai-configs/3/video-probe', '/api/v1/ai-configs/3/video-probe/reference', '/api/v1/ai-configs/3/video-probe',
  ])
  const upload = fetch.mock.calls[1][1]
  expect((upload.body as FormData).get('file')).toMatchObject({ name: '角色.png' })
  expect(upload).toMatchObject({ credentials: 'include', signal: scope.signal, headers: { 'X-Editor-Actor-Id': '4' } })
  expect(JSON.parse(String(fetch.mock.calls[2][1].body))).toEqual({ image_ids: ['image-uuid'] })
})
