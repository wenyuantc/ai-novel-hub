import { createElement, type EffectCallback, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Project } from '../../api/types'
import { ApiError } from '../../api/client'
import { castingContentChanged, readerApi, type ReaderAICastingResult } from '../../api/reader'
import { deferred } from '../systemSettings/systemSettingsTestFixtures'
import ReaderAICasting from './ReaderAICasting'

const view = vi.hoisted(() => ({
  source: 'own', submit: null as (() => Promise<void>) | null, effects: [] as EffectCallback[],
}))
vi.mock('react', async (original) => {
  const actual = await original<typeof import('react')>()
  return { ...actual, useLayoutEffect: (fn: EffectCallback) => { view.effects.push(fn) } }
})
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual, Button: (p: { onClick: () => Promise<void>; disabled?: boolean; children: ReactNode }) => {
    if (p.children === '分析本章并配音') view.submit = p.onClick
    return createElement('button', { disabled: p.disabled }, p.children)
  } }
})
vi.mock('../../hooks/useAIModelSource', () => ({ useAIModelSource: () => view.source }))
vi.mock('../AIConfigSelect', () => ({ default: () => null }))
const hash = 'c'.repeat(64)
const project = { id: 9, access: { authorization_version: 'g1' } } as Project
const cue = { kind: 'dialogue' as const, start: 2, end: 4, text: '正文', speaker: null, emotion: 'neutral' as const, paragraphStart: 0 }
const voices = [{ id: 'local', name: '小梅', lang: 'zh-CN' }]
const answer: ReaderAICastingResult = { content_hash: hash, cues: [{ start: 2, speaker: '小陈', emotion: 'happy' }],
  voices: [{ speaker: '@narrator', voice_id: 'local' }, { speaker: '小陈', voice_id: 'local' }] }
function render(current = () => true, options = { voices, cues: [cue], hasText: true }) {
  view.effects = []
  const save = vi.fn(() => true)
  const recheckAccess = vi.fn(async () => {})
  const page = renderToStaticMarkup(createElement(ReaderAICasting, { project, chapterId: 3, contentHash: hash,
    ...options, saveResult: save, reload: vi.fn(), sessionCurrent: current, recheckAccess }))
  return { save, page, recheckAccess, submit: view.submit! }
}
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); view.source = 'own' })
describe('显式发起 AI 配音分析', () => {
  it('打开面板不分析；点击后仅发送快照指纹、台词区间和本机候选，连点只请求一次', async () => {
    const slow = deferred<ReaderAICastingResult>()
    const request = vi.spyOn(readerApi, 'aiCasting').mockReturnValue(slow.promise)
    const { save, page, submit } = render()
    expect(page).toContain('本章已保存的正文')
    expect(request).not.toHaveBeenCalled()
    const first = submit()
    await submit()
    expect(request).toHaveBeenCalledTimes(1)
    expect(request.mock.calls[0].slice(0, 3)).toEqual([9, 3, { ai_config_id: null, content_hash: hash, cues: [{ start: 2, end: 4 }], voices }])
    slow.resolve(answer)
    await first
    expect(save).toHaveBeenCalledExactlyOnceWith(answer)
  })
  it.each(['logout', 'close'] as const)('退出账号或关闭抽屉后忽略迟到结果（%s）', async (mode) => {
    const slow = deferred<ReaderAICastingResult>()
    const request = vi.spyOn(readerApi, 'aiCasting').mockReturnValue(slow.promise)
    let current = true
    const { save, submit } = render(() => current)
    vi.stubGlobal('window', { addEventListener: vi.fn(), removeEventListener: vi.fn() })
    const cleanups = view.effects.map((fn) => fn())
    const wait = submit()
    if (mode === 'logout') current = false
    else for (const cleanup of cleanups) if (typeof cleanup === 'function') cleanup()
    const [, , , signal, active] = request.mock.calls[0]
    expect(active()).toBe(false)
    if (mode === 'close') expect(signal.aborted).toBe(true)
    slow.resolve(answer)
    await wait
    expect(save).not.toHaveBeenCalled()
  })
  it('指纹、台词或音色返回不匹配时保留原设置', async () => {
    const request = vi.spyOn(readerApi, 'aiCasting')
    for (const response of [{ ...answer, content_hash: 'other' }, { ...answer, cues: [] }, { ...answer, voices: [{ speaker: '小陈', voice_id: 'remote' }] }]) {
      request.mockResolvedValueOnce(response)
      const { submit, save } = render()
      await submit()
      expect(save).not.toHaveBeenCalled()
    }
  })
  it('纯旁白章也可分析，只采纳旁白的推荐音色', async () => {
    const result = { ...answer, cues: [], voices: [answer.voices[0]] }
    const request = vi.spyOn(readerApi, 'aiCasting').mockResolvedValueOnce(result)
    const { submit, save, page } = render(() => true, { voices, cues: [], hasText: true })
    expect(page).toContain('模型将为旁白选声')
    await submit()
    expect(request.mock.calls[0][2].cues).toEqual([])
    expect(save).toHaveBeenCalledExactlyOnceWith(result)
  })
  it.each([new ApiError(403, '没有权限'), new ApiError(409, '授权已变化', { code: 'permission_denied' })])('配音 AI 拒绝访问时再次核对作品读权限', async (error) => {
    vi.spyOn(readerApi, 'aiCasting').mockRejectedValueOnce(error)
    const { submit, recheckAccess } = render()
    await submit()
    expect(recheckAccess).toHaveBeenCalledOnce()
  })
  it('仅明确的正文冲突要求刷新章节，模型忙碌等 409 可以重试', () => {
    expect(castingContentChanged(new ApiError(409, '模型忙碌'))).toBe(false)
    expect(castingContentChanged(new ApiError(409, '正文变更', { code: 'reader_casting_content_changed' }))).toBe(true)
  })
  it('空章节、没有本机声音或没有可用模型时不能调用 AI', async () => {
    const request = vi.spyOn(readerApi, 'aiCasting')
    for (const opts of [{ voices: [], cues: [cue], hasText: true }, { voices, cues: [], hasText: false }, { voices, cues: [cue], hasText: true }]) {
      if (opts.voices.length && opts.cues.length) view.source = 'none'
      const { submit, page } = render(() => true, opts)
      expect(page).toContain('<button disabled="">分析本章并配音')
      await submit()
    }
    expect(request).not.toHaveBeenCalled()
  })
})
