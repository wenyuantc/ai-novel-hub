import { afterEach, describe, expect, it, vi } from 'vitest'
import { applyThumbnailAI, generateThumbnailAI, loadThumbnailPrompt } from './settingThumbnailAi'
import { thumbnailModelChoice } from '../components/entities/thumbnailGeneration'
import type { ThumbnailTarget } from './settingThumbnails'
import type { AIConfig, ProjectAccess } from './types'
import { writeResultUnknown } from '../utils/writeResult'

const project: ThumbnailTarget = { scope: 'project', projectId: 1, kind: 'character', entryId: 4 }
const series: ThumbnailTarget = { scope: 'series', seriesId: 2, entryId: 7 }
const image = { version: 'new', mime_type: 'image/webp', width: 200, height: 300 }
const editor = { role: 'editor', is_collaborator: true, allow_owner_ai: true } as ProjectAccess
const config = { id: 3, model_type: 'image', is_default: true } as AIConfig

afterEach(() => vi.unstubAllGlobals())

describe('缩略图 AI 接口', () => {
  it('作品和系列请求携带原始图片版本、系列修订号及确认凭据', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      if (url.endsWith('/ai/prompt')) return new Response(JSON.stringify({ prompt: '画像', prompt_token: 'prompt-token' }))
      if (url.endsWith('/ai/generate')) return new Response(JSON.stringify({ image_base64: 'YWJj', mime_type: 'image/webp', model_source: 'actor', preview_token: 'preview-token' }))
      return new Response(JSON.stringify(url.includes('/series/') ? { thumbnail: image, revision: 9 } : image))
    })
    const current = () => true
    const signal = new AbortController().signal
    expect(await loadThumbnailPrompt(project, current, signal)).toEqual({ prompt: '画像', prompt_token: 'prompt-token' })
    await generateThumbnailAI(series, { prompt: '夜景', prompt_token: 'prompt-token', config_id: 3, expected_version: '', expected_revision: 8 }, 'actor', current, signal)
    await applyThumbnailAI(series, { image_base64: 'YWJj', preview_token: 'preview-token' }, current, signal)
    expect(calls.map((call) => call.url)).toEqual([
      '/api/v1/projects/1/settings/character/4/thumbnail/ai/prompt',
      '/api/v1/series/2/entries/7/thumbnail/ai/generate',
      '/api/v1/series/2/entries/7/thumbnail/ai/apply',
    ])
    expect(JSON.parse(calls[1].init.body as string)).toEqual({ prompt: '夜景', prompt_token: 'prompt-token', config_id: 3, expected_version: '', expected_revision: 8 })
    expect(JSON.parse(calls[2].init.body as string)).toEqual({ image_base64: 'YWJj', preview_token: 'preview-token' })
    expect(calls.every(({ init }) => init.credentials === 'include')).toBe(true)
  })

  it('系列仅选择本人模型，作品只有允许的协作者才能回退作者', () => {
    expect(thumbnailModelChoice(series, editor, true, [], null)).toMatchObject({ mode: 'missing', allowed: false })
    expect(thumbnailModelChoice(series, editor, true, [config], null)).toMatchObject({ mode: 'actor', configId: 3 })
    expect(thumbnailModelChoice(project, editor, true, [], null)).toMatchObject({ mode: 'owner', allowed: true })
    expect(thumbnailModelChoice(project, editor, true, [], 3)).toMatchObject({ mode: 'invalid', allowed: false })
  })

  it('旧作用域的迟到响应被丢弃，错误来源也不能进入预览', async () => {
    let valid = true
    let finish!: (response: Response) => void
    const pending = new Promise<Response>((resolve) => { finish = resolve })
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(pending).mockResolvedValueOnce(new Response(JSON.stringify({
      image_base64: 'YWJj', mime_type: 'image/webp', model_source: 'owner', preview_token: 'p',
    }))))
    const signal = new AbortController().signal
    const request = generateThumbnailAI(project, { prompt: '画像', prompt_token: 't', config_id: 3, expected_version: '' }, 'actor', () => valid, signal)
    valid = false
    finish(new Response(JSON.stringify({ image_base64: 'YWJj', mime_type: 'image/webp', model_source: 'actor', preview_token: 'p' })))
    expect(await request).toBeNull()
    await expect(generateThumbnailAI(series, { prompt: '画像', prompt_token: 't', config_id: 3, expected_version: '', expected_revision: 1 }, 'actor', () => true, signal))
      .rejects.toThrow('模型来源')
  })

  it('提示词、确认结果及新请求均受原作用域保护，确认失败不自动重放', async () => {
    let current = true
    let finish!: (response: Response) => void
    const pending = new Promise<Response>((resolve) => { finish = resolve })
    const fetch = vi.fn().mockReturnValueOnce(pending)
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: '操作未确认' }), { status: 503 }))
    vi.stubGlobal('fetch', fetch)
    const signal = new AbortController().signal
    const prompt = loadThumbnailPrompt(project, () => current, signal)
    current = false
    finish(new Response(JSON.stringify({ prompt: '旧提示词', prompt_token: 'old' })))
    expect(await prompt).toBeNull()
    expect(await applyThumbnailAI(project, { image_base64: 'YWJj', preview_token: 'p' }, () => current, signal)).toBeNull()
    expect(fetch).toHaveBeenCalledOnce()
    current = true
    await expect(applyThumbnailAI(project, { image_base64: 'YWJj', preview_token: 'p' }, () => current, signal))
      .rejects.toThrow('操作未确认')
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('图片类型和实际模型来源必须与本次请求一致', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ image_base64: 'YWJj',
      mime_type: 'image/png', model_source: 'actor', preview_token: 'p' }))))
    const signal = new AbortController().signal
    await expect(generateThumbnailAI(project, { prompt: '画像', prompt_token: 't', config_id: 3, expected_version: '' },
      'actor', () => true, signal)).rejects.toThrow('图片或模型来源无法确认')
  })

  it('确认接口成功但响应损坏时视为提交结果未知，需要重新读取而不能重复保存', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ unexpected: true })))
    vi.stubGlobal('fetch', fetch)
    let failure: unknown
    try {
      await applyThumbnailAI(project, { image_base64: 'YWJj', preview_token: 'p' }, () => true, new AbortController().signal)
    } catch (error) { failure = error }
    expect(failure).toBeInstanceOf(Error)
    expect(writeResultUnknown(failure)).toBe(true)
    expect(fetch).toHaveBeenCalledOnce()
  })
})
