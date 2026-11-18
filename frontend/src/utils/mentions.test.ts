import { describe, expect, it } from 'vitest'
import { MENTION_CATEGORY_LABELS } from '../api/mentions'
import { entityDetailHref } from './entityLinks'
import { bookSearchHref, mentionHref, mentionSegments, readMentionAnchor, resolveEntityDeepLink, staleAnchorMessage } from './mentions'

describe('提及原文定位', () => {
  it('九类提及有类型标签，同 ID 的不同设定仍跳到各自的详情入口', () => {
    expect(Object.keys(MENTION_CATEGORY_LABELS).sort()).toEqual(['career', 'character', 'faction', 'history', 'item', 'organization', 'other', 'place', 'rule'])
    for (const [kind, path] of [['character', 'characters'], ['world', 'world'], ['organization', 'organizations'], ['career', 'careers']] as const) {
      expect(entityDetailHref(2, { kind, id: 7 })).toBe(`/projects/2/${path}?id=7`)
    }
  })
  it('首次加载失败保留深链，成功重试后才判断目标存在性', () => {
    const entity = { id: 7, project_id: 2 }
    expect(resolveEntityDeepLink(2, undefined, false, '7')).toEqual({ status: 'waiting' })
    expect(resolveEntityDeepLink(2, undefined, true, '7')).toEqual({ status: 'waiting' })
    expect(resolveEntityDeepLink(2, [entity], false, '7')).toEqual({ status: 'found', item: entity })
    expect(resolveEntityDeepLink(2, [], false, '7')).toEqual({ status: 'missing' })
    expect(resolveEntityDeepLink(3, [entity], false, '7')).toEqual({ status: 'missing' })
  })
  it('码点范围保留 emoji、空白与原文，不把窗口偏移当局部位置', () => {
    const parts = mentionSegments(' 😀林远\r\n', [{ start: 12, end: 14, text: '林远', entities: [] }], 10, { start: 12, end: 14 })
    expect(parts.map((p) => p.text).join('')).toBe(' 😀林远\r\n')
    expect(parts.find((p) => p.focused)?.text).toBe('林远')
  })
  describe('焦点高亮严格等于焦点区间', () => {
    const text = '😀林远的佩剑被巨石砸断。他只得赤手下山。'
    const entity = (id: number, name: string) => ({ kind: 'character' as const, id, name, category: 'character' as const, aliases: [] })
    const span = (start: number, end: number, id = 1) => ({ start, end, text: Array.from(text).slice(start, end).join(''), entities: [entity(id, '设定')] })
    const focusedText = (parts: ReturnType<typeof mentionSegments>) => parts.filter((p) => p.focused).map((p) => p.text).join('')
    const check = (parts: ReturnType<typeof mentionSegments>, focus: { start: number; end: number }) => {
      expect(parts.map((p) => p.text).join('')).toBe(text)
      expect(focusedText(parts)).toBe(Array.from(text).slice(focus.start, focus.end).join(''))
      for (const part of parts.filter((p) => p.focused)) expect(part.start >= focus.start && part.end <= focus.end).toBe(true)
    }

    it('焦点包含一个提及：高亮止于引句末尾，提及仍可点击', () => {
      const focus = { start: 1, end: 12 }
      const parts = mentionSegments(text, [span(1, 3)], 0, focus)
      check(parts, focus)
      expect(focusedText(parts)).toBe('林远的佩剑被巨石砸断。')
      expect(parts.find((p) => p.text === '林远')).toMatchObject({ focused: true, entities: [{ id: 1 }] })
      expect(parts[parts.length - 1]).toMatchObject({ text: '他只得赤手下山。', focused: false })
    })
    it('焦点与提及部分重叠：提及在焦点边界切开，两段都保留设定，只有焦点内的一段高亮', () => {
      const focus = { start: 2, end: 6 }
      const parts = mentionSegments(text, [span(1, 3), span(4, 8, 2)], 0, focus)
      check(parts, focus)
      expect(parts.filter((p) => p.entities.length > 0).map((p) => [p.text, p.focused, p.entities[0].id])).toEqual([
        ['林', false, 1], ['远', true, 1], ['佩剑', true, 2], ['被巨', false, 2],
      ])
    })
    it('焦点内含多个提及：提及与其间的正文都高亮，焦点之外不高亮', () => {
      const focus = { start: 1, end: 12 }
      const parts = mentionSegments(text, [span(1, 3), span(4, 6, 2), span(13, 14, 3)], 0, focus)
      check(parts, focus)
      expect(parts.filter((p) => p.entities.length > 0).map((p) => [p.text, p.focused])).toEqual([['林远', true], ['佩剑', true], ['只', false]])
    })
    it('没有提及：只高亮焦点区间', () => {
      const focus = { start: 4, end: 6 }
      const parts = mentionSegments(text, [], 0, focus)
      check(parts, focus)
      expect(parts.map((p) => [p.text, p.focused])).toEqual([['😀林远的', false], ['佩剑', true], ['被巨石砸断。他只得赤手下山。', false]])
    })
    it('分块渲染（窗口偏移）：焦点跨出本块时只高亮落在本块内的部分；无效焦点不高亮', () => {
      const block = Array.from(text).slice(8, 20).join('')
      const parts = mentionSegments(block, [], 8, { start: 1, end: 12 })
      expect(parts.map((p) => [p.text, p.focused])).toEqual([['石砸断。', true], ['他只得赤手下山。', false]])
      expect(mentionSegments(block, [], 8, { start: 30, end: 40 }).some((p) => p.focused)).toBe(false)
      expect(mentionSegments(text, [span(1, 3)], 0, { start: 6, end: 2 }).some((p) => p.focused)).toBe(false)
    })
  })
  it('提及与一致性深链保留原有失效提示，忽略不认识的来源', () => {
    const base = `mention_start=1&mention_end=3&mention_hash=${'a'.repeat(64)}`
    expect(readMentionAnchor(new URLSearchParams(base))).toEqual({ start: 1, end: 3, content_hash: 'a'.repeat(64) })
    expect(readMentionAnchor(new URLSearchParams(`${base}&mention_from=other`))).toEqual({ start: 1, end: 3, content_hash: 'a'.repeat(64) })
    expect(readMentionAnchor(new URLSearchParams(`${base}&mention_from=consistency`))?.origin).toBe('consistency')
    expect(staleAnchorMessage({}, '原文已变化，请重新查看提及列表定位。')).toBe('原文已变化，请重新查看提及列表定位。')
    expect(staleAnchorMessage(null, '原措辞')).toBe('原措辞')
    expect(staleAnchorMessage({ origin: 'consistency' }, '原措辞')).toContain('请回到来源页面重新定位')
  })
  it('全书搜索链接用章内码点和指纹定位，可区分同段的不同命中', () => {
    const match = { start: 13, end: 16, content_hash: 'b'.repeat(64) }
    const url = new URL(bookSearchHref(12, 7, match), 'https://reader.test')
    expect(url.pathname).toBe('/projects/12/read/7')
    const anchor = readMentionAnchor(url.searchParams)
    expect(anchor).toEqual({ ...match, origin: 'book-search' })
    expect(staleAnchorMessage(anchor, '提及')).toContain('重新搜索')
  })
  it('拒绝负数、反向范围和缺少原文指纹的旧位置', () => {
    expect(readMentionAnchor(new URLSearchParams('mention_start=-1&mention_end=3'))).toBeNull()
    expect(readMentionAnchor(new URLSearchParams(`mention_start=4&mention_end=2&mention_hash=${'a'.repeat(64)}`))).toBeNull()
    expect(readMentionAnchor(new URLSearchParams('mention_start=1&mention_end=3'))).toBeNull()
  })
  it('四来源链接保留稳定身份，URL不含正文片段', () => {
    const anchor = { start: 1, end: 3, content_hash: 'a'.repeat(64) }
    const source = { type: 'chat' as const, id: 9, title: '私有标题', chapter_no: null, conversation_id: 4, created_at: null }
    const url = mentionHref(2, source, anchor)
    expect(url).toContain('conversation_id=4')
    expect(url).toContain('message_id=9')
    expect(url).not.toContain('私有标题')
    expect(readMentionAnchor(new URLSearchParams(url.split('?')[1]))).toEqual(anchor)
  })
})
