import { describe, expect, it } from 'vitest'
import { mentionHref, mentionSegments, readMentionAnchor, resolveEntityDeepLink } from './mentions'

describe('提及原文定位', () => {
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
