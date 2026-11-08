import { describe, expect, it } from 'vitest'
import { chapterLeaveBlocked, mergeNoteFlags, noteDraftError, noteDirty, noteId, noteListEmptyText, noteSearch, noteTargetKey, sideNoteSwitch } from './noteDraft'

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

describe('笔记标记与笔记栏', () => {
  it('切换笔记栏目标时只在有未保存修改时确认', () => {
    const current = { key: 'note:3', id: 3 }
    expect(sideNoteSwitch(current, 3, true)).toBe('same')
    expect(sideNoteSwitch(current, 4, false)).toBe('switch')
    expect(sideNoteSwitch(current, 4, true)).toBe('confirm')
    expect(sideNoteSwitch(current, 'new', true)).toBe('confirm')
    expect(sideNoteSwitch(current, null, true)).toBe('confirm')
    expect(sideNoteSwitch(null, null, false)).toBe('same')
    expect(sideNoteSwitch({ key: 'new:1', id: null }, 'new', false)).toBe('switch')
  })

  it('合并标记不改变修订号、时间与正文', () => {
    const saved = { id: 3, title: '旧', content: '草', revision: 2, updated_at: 'a', is_favorite: false, is_pinned: false }
    const merged = mergeNoteFlags(saved, { is_favorite: false, is_pinned: true })
    expect(merged).toEqual({ ...saved, is_pinned: true })
  })

  it('空状态文案区分收藏筛选与搜索', () => {
    expect(noteListEmptyText('', false)).toContain('还没有笔记')
    expect(noteListEmptyText('', true)).toBe('没有收藏的笔记')
    expect(noteListEmptyText('线索', false)).toBe('没有找到相关笔记')
    expect(noteListEmptyText('线索', true)).toBe('没有找到相关的收藏笔记')
  })

  it('笔记草稿只拦截离开编辑页，不拦截切换场景', () => {
    const at = (pathname: string, search = '') => ({ pathname, search })
    const here = at('/p/1/chapters/3', '?scene_id=1')
    expect(chapterLeaveBlocked(false, true, here, at('/p/1/chapters/3', '?scene_id=2'))).toBe(false)
    expect(chapterLeaveBlocked(false, true, here, at('/p/1/notes'))).toBe(true)
    expect(chapterLeaveBlocked(true, false, here, at('/p/1/chapters/3', '?scene_id=2'))).toBe(true)
    expect(chapterLeaveBlocked(true, false, here, at('/p/1/chapters/3', '?scene_id=1&panel=x'))).toBe(false)
    expect(chapterLeaveBlocked(false, false, here, at('/p/1/notes'))).toBe(false)
  })
})
