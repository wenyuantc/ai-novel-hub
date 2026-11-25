import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { AnnotationThread } from '../../api/annotations'
import { AnnotationDiscussion } from './AnnotationPanel'

const first = { id: 7, author_name: '历史署名', created_at: '2026-11-25T10:00:00Z', body: '正文里提到哪一天？' }
const second = { ...first, id: 8, body: '<b>第二日</b>', author_name: '另一位协作者' }
vi.mock('../../hooks/useMentionQuery', () => ({
  useMentionQuery: () => ({ data: { total: 2, items: [first, second] }, reload: vi.fn() }),
}))
const thread: AnnotationThread = {
  id: 1, scene_id: 2, chapter_id: 3, content_revision: 4, start: 4, end: 6,
  excerpt: '清晨', selection_hash: 'a'.repeat(64), anchor_valid: true, resolved: false,
  created_at: first.created_at, first_message: first,
}
function render(owner: boolean, item = thread) {
  return renderToStaticMarkup(createElement(AnnotationDiscussion, {
    pid: 1, cid: 3, item, owner, current: () => true, onJump: vi.fn(), onResolve: vi.fn(),
  }))
}

describe('章节工作台的批注讨论', () => {
  it('查看者能回复和跳原句，只有作者有处理入口，留言以纯文本显示', () => {
    const html = render(false)
    expect(html).toContain('跳到原文')
    expect(html).toContain('发表回复')
    expect(html).not.toContain('标记已处理')
    expect(html.indexOf('历史署名')).toBeLessThan(html.indexOf('另一位协作者'))
    expect(html).toContain('&lt;b&gt;第二日&lt;/b&gt;')
    expect(render(true)).toContain('标记已处理')
  })
  it('原句失效和处理状态独立，意见和回复保留', () => {
    const stale = { ...thread, anchor_valid: false, start: null, end: null }
    expect(render(true, stale)).toContain('标记已处理')
    const html = render(true, { ...stale, resolved: true })
    for (const label of ['原文位置已失效', '已处理', '清晨', '正文里提到哪一天？', '另一位协作者']) expect(html).toContain(label)
    expect(html).not.toContain('跳到原文')
    expect(html).not.toContain('标记已处理')
  })
})
