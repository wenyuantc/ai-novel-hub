import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { aiSettingsNeedsReview, settingAiVisibilityApi } from './settingAiVisibility'

afterEach(() => vi.unstubAllGlobals())

describe('AI 与提及设置客户端', () => {
  it('按独立条目路径读取、写配置及旧字段可见性，保留会话、版本和空数组', async () => {
    const calls: { url: string; init: RequestInit }[] = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => { calls.push({ url, init }); return new Response('{}') })
    const signal = new AbortController().signal
    const entry = { kind: 'career', id: 9 } as const
    await settingAiVisibilityApi.get(2, entry, signal)
    await settingAiVisibilityApi.update(2, entry, { expected_ai_visibility_revision: 6, ai_mode: 'never', links: [], mention_enabled: false }, signal)
    await settingAiVisibilityApi.updateLegacyField(2, entry, 17, { expected_ai_visibility_revision: 7, expected_timeline_revision: 4, ai_hidden: true }, signal)
    const path = '/api/v1/projects/2/setting-entries/career/9'
    expect(calls.map(({ url, init }) => [url, init.method])).toEqual([
      [`${path}/ai-settings`, 'GET'], [`${path}/ai-settings`, 'PATCH'], [`${path}/legacy-fields/17/ai-visibility`, 'PATCH'],
    ])
    expect(JSON.parse(String(calls[1].init.body))).toEqual({ expected_ai_visibility_revision: 6, ai_mode: 'never', links: [], mention_enabled: false })
    expect(JSON.parse(String(calls[2].init.body))).toEqual({ expected_ai_visibility_revision: 7, expected_timeline_revision: 4, ai_hidden: true })
    expect(calls.every(({ init }) => init.signal === signal && init.credentials === 'include')).toBe(true)
  })

  it('区分版本冲突、结果不明和未保存的屏障忙，不自动重发', () => {
    expect(aiSettingsNeedsReview(new ApiError(409, '版本不符', { code: 'stale_revision' }))).toBe(true)
    expect(aiSettingsNeedsReview(new ApiError(0, '断线'))).toBe(true)
    expect(aiSettingsNeedsReview(new ApiError(409, '门禁忙', { code: 'ai_dispatch_busy' }))).toBe(false)
    expect(aiSettingsNeedsReview(new ApiError(422, '校验失败'))).toBe(false)
  })
})
