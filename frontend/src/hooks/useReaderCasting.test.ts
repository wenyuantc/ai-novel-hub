import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { ReaderAICastingResult } from '../api/reader'
import type { ReaderPassage } from '../utils/readerDialogue'
import { applyReaderAiCues, applyReaderCastOverrides, castingStore, useReaderCasting } from './useReaderCasting'

const parse = (value: unknown) => typeof value === 'string' ? value : undefined
let data: Map<string, string>

beforeEach(() => {
  data = new Map()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => data.set(key, value),
  })
})
afterEach(() => vi.unstubAllGlobals())

function read(actor: number, project: number, chapter: number, hash: string) {
  let result!: ReturnType<typeof useReaderCasting>
  function Reader() { result = useReaderCasting(actor, project, chapter, hash); return null }
  renderToStaticMarkup(createElement(Reader))
  return result
}

describe('本机角色配音设置', () => {
  it('角色音色同书跨章保留，台词纠正仅同账号、同书、同章及同正文版本可见', () => {
    const first = read(1, 2, 3, '原稿')
    first.setVoice('阿青', '本机音色')
    first.setOverride(5, { speaker: '阿青', emotion: 'angry' })
    expect(read(1, 2, 3, '原稿').overrides[5]).toEqual({ speaker: '阿青', emotion: 'angry' })
    for (const other of [read(1, 2, 4, '原稿'), read(1, 2, 3, '改稿')]) {
      expect(other.voices['阿青']).toBe('本机音色')
      expect(other.overrides[5]).toBeUndefined()
    }
    for (const other of [read(9, 2, 3, '原稿'), read(1, 9, 3, '原稿')]) {
      expect(other.voices['阿青']).toBeUndefined()
      expect(other.overrides[5]).toBeUndefined()
    }
  })

  it('合并同一作品两个标签页的设置，支持同名对象属性的角色', () => {
    const first = castingStore('book', '1', parse)
    const second = castingStore('book', '1', parse)
    first.set('__proto__', '甲')
    second.set('林夏', '乙')
    expect(second.snapshot()['__proto__']).toBe('甲')
    first.set('__proto__', undefined)
    expect(castingStore('book', '1', parse).snapshot()).toEqual({ 林夏: '乙' })
    expect(castingStore('other-book', '1', parse).snapshot()).toEqual({})
  })

  it('存储写满后仍保留本页的多次纠正', () => {
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('quota') })
    const store = castingStore('chapter', '1', parse)
    store.set('12', '林夏')
    store.set('31', '小青')
    expect(store.snapshot()).toEqual({ 12: '林夏', 31: '小青' })
  })
})


describe('AI 配音结果', () => {
  const generated: ReaderAICastingResult = {
    content_hash: '原稿',
    cues: [{ start: 5, speaker: '林夏', emotion: 'happy' }],
    voices: [{ speaker: '林夏', voice_id: '音色甲' }, { speaker: '@narrator', voice_id: '音色乙' }],
  }
  const detected: ReaderPassage[] = [
    { kind: 'narration', text: '他走过来。', speaker: null, emotion: 'neutral', start: 0, end: 5, paragraphStart: 0 },
    { kind: 'dialogue', text: '“早。”', speaker: null, emotion: 'neutral', start: 5, end: 9, paragraphStart: 0 },
  ]

  it('同一账号、作品、章节、指纹下刷新可恢复；其余范围均不读取旧建议', () => {
    const cast = read(1, 2, 3, '原稿')
    expect(cast.saveAiResult(generated)).toBe(true)
    const restored = read(1, 2, 3, '原稿')
    expect(restored.aiResult).toEqual(generated)
    expect(restored.effectiveVoices).toEqual({ 林夏: '音色甲', '@narrator': '音色乙' })
    for (const other of [read(2, 2, 3, '原稿'), read(1, 3, 3, '原稿'), read(1, 2, 4, '原稿'), read(1, 2, 3, '改稿')]) {
      expect(other.aiResult).toBeNull()
      expect(other.effectiveVoices).toEqual({})
    }
  })

  it('AI 重试采用新情绪、未手选的声音，保留请求进行期间的手动选声和角色纠正', () => {
    const requested = read(1, 2, 3, '原稿')
    requested.saveAiResult(generated)
    const editing = read(1, 2, 3, '原稿')
    editing.setVoice('林夏', '自己选的声音')
    editing.setOverride(5, { speaker: '小青' })
    const revised = { ...generated, cues: [{ start: 5, speaker: '阿禾', emotion: 'sad' as const }],
      voices: [{ speaker: '林夏', voice_id: '音色丙' }, { speaker: '@narrator', voice_id: '音色丁' }] }
    requested.saveAiResult(revised)
    const restored = read(1, 2, 3, '原稿')
    expect(restored.effectiveVoices).toEqual({ 林夏: '自己选的声音', '@narrator': '音色丁' })
    const inferred = applyReaderAiCues(detected, restored.aiResult)
    expect(inferred[0]).toBe(detected[0])
    expect(applyReaderCastOverrides(inferred, restored.overrides)[1]).toEqual({ ...detected[1], speaker: '小青', emotion: 'sad' })
    restored.setVoice('林夏', '')
    restored.setOverride(5, { emotion: 'angry' })
    const reset = read(1, 2, 3, '原稿')
    expect(reset.effectiveVoices['林夏']).toBe('音色丙')
    expect(applyReaderCastOverrides(inferred, reset.overrides)[1]).toEqual({ ...detected[1], speaker: '阿禾', emotion: 'angry' })
  })

  it('拒绝不匹配指纹及损坏结果，保留最后一次成功的配音；缓存不保存附加正文', () => {
    const cast = read(1, 2, 3, '原稿')
    const untrusted = { ...generated, body: '正文不得进入缓存', cues: [{ ...generated.cues[0], text: '正文不得进入缓存' }] }
    expect(cast.saveAiResult(untrusted)).toBe(true)
    expect(cast.saveAiResult({ ...generated, content_hash: '旧稿' })).toBe(false)
    expect(cast.saveAiResult({ ...generated, cues: [...generated.cues, ...generated.cues] })).toBe(false)
    expect([...data.values()].join('')).not.toContain('正文不得进入缓存')
    expect(read(1, 2, 3, '原稿').aiResult).toEqual(generated)
    expect(read(1, 2, 3, '').saveAiResult({ ...generated, content_hash: '' })).toBe(false)
  })
})
