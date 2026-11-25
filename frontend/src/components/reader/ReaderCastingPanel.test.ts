import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { ReaderAICastingResult } from '../../api/reader'
import type { ReaderPassage } from '../../utils/readerDialogue'
import { DIALOGUE_ROLE, NARRATOR_ROLE } from '../../utils/readerVoices'
import ReaderCastingPanel from './ReaderCastingPanel'

const detected: ReaderPassage[] = [
  { start: 0, end: 4, paragraphStart: 0, kind: 'narration', text: '阿青说：', speaker: null, emotion: 'neutral' },
  { start: 4, end: 8, paragraphStart: 0, kind: 'dialogue', text: '“别怕”', speaker: '阿青', emotion: 'neutral' },
  { start: 9, end: 13, paragraphStart: 9, kind: 'dialogue', text: '“好呀”', speaker: null, emotion: 'happy' },
]
const script: ReaderPassage[] = [detected[0], { ...detected[1], speaker: '小月', emotion: 'whisper' }, detected[2]]
const localVoices = [{ id: 'one', name: '晓梅', lang: 'zh-CN' }, { id: 'two', name: '明远', lang: 'zh-TW' }]
function render(status: 'idle' | 'playing', voices = localVoices, aiResult: ReaderAICastingResult | null = null) {
  return renderToStaticMarkup(createElement(ReaderCastingPanel, {
    detected, script,
    casting: { voices: { [NARRATOR_ROLE]: 'removed', '小月': 'two' }, overrides: { '4': { speaker: '小月', emotion: 'whisper' } },
      aiResult, saveAiResult: vi.fn(), effectiveVoices: { [NARRATOR_ROLE]: 'removed', '小月': 'two' },
      setOverride: vi.fn(), setVoice: vi.fn() },
    player: { voices, status, message: null, preview: vi.fn(), previewPassage: vi.fn(), previewing: false, stop: vi.fn() },
  }))
}

describe('阅读页角色配音面板', () => {
  it('按纠正后的角色配音，保留识别结果，并为失效声音提供重选入口', () => {
    const html = render('idle')
    expect(html).toContain('已选声音暂不可用')
    expect(html).toMatch(/<button[^>]*aria-label="试听旁白"[^>]*disabled/)
    expect(html).toContain('aria-label="小月的声音"')
    expect(html).toContain('aria-label="未指定角色的声音"')
    expect(html).toMatch(/<option value="role:小月" selected="">小月/)
    expect(html).toMatch(/<option value="whisper" selected="">轻声/)
    expect(html).toContain('自动 · 阿青')
    expect(html).toContain(`value="role:${DIALOGUE_ROLE}"`)
    expect(html).toContain('新增角色…')
    expect(html.match(/试听这句/g)).toHaveLength(2)
  })

  it('同时展示 AI 推荐来源和当前手动选择', () => {
    const html = render('idle', localVoices, { content_hash: 'h', cues: [{ start: 4, speaker: '阿青', emotion: 'neutral' }],
      voices: [{ speaker: '小月', voice_id: 'one' }] })
    expect(html).toContain('AI 推荐 · 晓梅')
    expect(html).toContain('AI 识别 · 阿青')
    expect(html).toMatch(/<option value="two" selected="">明远/)
    expect(html).toMatch(/<option value="whisper" selected="">轻声/)
  })

  it.each([['playing', localVoices], ['idle', []]] as const)('播放中或无声音时保留设置并禁用试听', (status, voices) => {
    const html = render(status, [...voices])
    const previewButtons = [...html.matchAll(/<button([^>]*)>([\s\S]*?)<\/button>/g)]
      .filter((m) => m[0].includes('试听'))
    expect(previewButtons).toHaveLength(5)
    for (const button of previewButtons) expect(button[1]).toContain('disabled')
    expect(html).toContain('新增角色…')
  })
})
