import { describe, expect, it } from 'vitest'
import { noteDraftError, noteDirty, noteId, noteSearch, noteTargetKey } from './noteDraft'

describe('作品笔记草稿与导航', () => {
  it('拒绝损坏或不安全的笔记ID', () => {
    expect(noteId('31')).toBe(31)
    for (const value of [null, '', '0', '-1', '1.5', '01', 'abc', '9007199254740992']) expect(noteId(value)).toBeNull()
  })

  it('按码点限制文本，保存前只规范化标题而不破坏正文', () => {
    expect(noteDraftError({ title: '  ', content: '' })).toBe('请输入笔记标题')
    expect(noteDraftError({ title: '😀'.repeat(200), content: '😀'.repeat(200000) })).toBeNull()
    expect(noteDraftError({ title: '😀'.repeat(201), content: '' })).toContain('200')
    expect(noteDraftError({ title: '正文', content: '😀'.repeat(200001) })).toContain('200000')
    const saved = { title: '笔记', content: ' 原文\r\n😀' }
    expect(noteDirty(saved, saved)).toBe(false)
    expect(noteDirty({ ...saved, content: saved.content.trim() }, saved)).toBe(true)
    expect(noteDirty({ title: '', content: '' }, null)).toBe(false)
  })

  it('关闭或切笔记保留无关参数，并区分真正离开与同笔记定位', () => {
    const search = '?id=3&mention_start=12&mention_end=14&mention_hash=abc&agent=open'
    expect(noteSearch(search, 8)).toBe('agent=open&id=8')
    expect(noteSearch(search, 'new')).toBe('agent=open&new=1')
    expect(noteSearch(search, null)).toBe('agent=open')
    expect(noteTargetKey('/notes', search)).toBe(noteTargetKey('/notes', '?id=3&mention_start=2'))
    expect(noteTargetKey('/notes', search)).not.toBe(noteTargetKey('/notes', '?id=4'))
  })
})
