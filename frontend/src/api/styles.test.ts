import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { promptApi } from './prompts'
import { extractStyle, parseExtractDone, styleApi, trialStyle } from './styles'

interface Call {
  url: string
  init: RequestInit
}

function stubFetch(status: number, body?: unknown): Call[] {
  const calls: Call[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(status === 204 || body === undefined ? null : JSON.stringify(body), { status })
  })
  return calls
}

function stubSSE(frames: string): Call[] {
  const calls: Call[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(frames, { status: 200, headers: { 'content-type': 'text/event-stream' } })
  })
  return calls
}

afterEach(() => vi.unstubAllGlobals())

describe('styleApi', () => {
  it('路径、方法与 force 查询参数', async () => {
    const calls = stubFetch(204)
    await styleApi.remove(3)
    await styleApi.remove(3, true)
    await styleApi.copy(3)
    await styleApi.export([1, 2])
    await styleApi.export()
    expect(calls.map((c) => [c.init.method, c.url])).toEqual([
      ['DELETE', '/api/v1/styles/3'],
      ['DELETE', '/api/v1/styles/3?force=true'],
      ['POST', '/api/v1/styles/3/copy'],
      ['GET', '/api/v1/styles/export?ids=1&ids=2'],
      ['GET', '/api/v1/styles/export'],
    ])
    expect(calls[0].init.credentials).toBe('include')
  })

  it('删除被引用的风格：409 的 references 保留在 ApiError.body', async () => {
    stubFetch(409, { detail: '该风格被 1 个项目、2 个章节引用', references: { projects: 1, chapters: 2 } })
    const err = await styleApi.remove(3).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).detail).toBe('该风格被 1 个项目、2 个章节引用')
    expect((err as ApiError).body?.references).toEqual({ projects: 1, chapters: 2 })
  })
})

describe('提取 / 试写（同步 SSE）', () => {
  it('提取：delta 流式回调，done 收窄为草稿', async () => {
    const calls = stubSSE(
      'event: delta\ndata: {"text":"{\\"name\\""}\n\nevent: done\ndata: {"draft":{"name":"冷","tone":"克制","pov":3},"warnings":["w",1]}\n\n',
    )
    const deltas: string[] = []
    const done = await extractStyle({ sample_text: 'x'.repeat(200) }, (t) => deltas.push(t))
    expect(calls[0].url).toBe('/api/v1/styles/extract')
    expect(calls[0].init.method).toBe('POST')
    expect(deltas).toEqual(['{"name"'])
    expect(done.draft.name).toBe('冷')
    expect(done.draft.tone).toBe('克制')
    expect(done.draft.pov).toBe('')
    expect(done.draft.extra).toBe('')
    expect(done.warnings).toEqual(['w'])
  })

  it('试写：error 事件抛出中文 detail', async () => {
    stubSSE('event: error\ndata: {"detail":"模型调用失败：额度不足"}\n\n')
    await expect(trialStyle({ style_id: 1, scene: '雨夜' }, () => {})).rejects.toThrow('模型调用失败：额度不足')
  })

  it('parseExtractDone 容错', () => {
    expect(parseExtractDone(null).draft.name).toBe('')
    expect(parseExtractDone(null).warnings).toEqual([])
  })
})

describe('promptApi', () => {
  it('路径与方法（key 编码）', async () => {
    const calls = stubFetch(204)
    await promptApi.save('chapter.generate', { system: 's', user: 'u', note: '' })
    await promptApi.patch('chapter.generate', { ack_default: true })
    await promptApi.reset('chapter.generate')
    await promptApi.restore('chapter.generate', 7)
    await promptApi.preview('chapter.generate', { user: 'u' })
    expect(calls.map((c) => [c.init.method, c.url])).toEqual([
      ['PUT', '/api/v1/prompt-templates/chapter.generate'],
      ['PATCH', '/api/v1/prompt-templates/chapter.generate'],
      ['DELETE', '/api/v1/prompt-templates/chapter.generate'],
      ['POST', '/api/v1/prompt-templates/chapter.generate/versions/7/restore'],
      ['POST', '/api/v1/prompt-templates/chapter.generate/preview'],
    ])
  })

  it('保存校验失败：errors 保留在 ApiError.body', async () => {
    stubFetch(400, { detail: '模板校验失败：用户段第 1 行：未知变量：x', errors: [{ part: 'user', line: 1, message: '未知变量：x' }] })
    const err = (await promptApi.save('chapter.generate', { user: '{{ x }}' }).catch((e: unknown) => e)) as ApiError
    expect(err.status).toBe(400)
    expect(err.body?.errors).toEqual([{ part: 'user', line: 1, message: '未知变量：x' }])
  })

  it('非 JSON 错误体：body 为 null，detail 回退', async () => {
    vi.stubGlobal('fetch', async () => new Response('oops', { status: 500 }))
    const err = (await promptApi.list().catch((e: unknown) => e)) as ApiError
    expect(err.detail).toBe('请求失败（HTTP 500）')
    expect(err.body).toBeNull()
  })
})
