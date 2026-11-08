import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { MarkerTimelineItem, MarkerTimelinePage } from '../../api/richDocument'
import { MarkerTimelineList } from './MarkerTimeline'

const active: MarkerTimelineItem = {
  key: 'shared-uuid', chapter_id: 1, chapter_no: 1, chapter_title: '启程', scene_id: 2, scene_title: '车站',
  scene_active: true, section_key: null, section_title: null, content_revision: 3,
  status: 'active', color: 'blue', start: 1, end: 3, last_start: null, excerpt: '正文选段', excerpt_truncated: false,
}
const stale: MarkerTimelineItem = {
  ...active, chapter_id: 2, chapter_no: 2, chapter_title: '归来', scene_id: 4, scene_title: '旧景',
  scene_active: false, section_key: 'retired-section', section_title: '旧备选',
  status: 'stale', color: 'red', start: null, end: null, last_start: 8,
  excerpt: '保留的旧片段', excerpt_truncated: true,
}
const page: MarkerTimelinePage = { items: [active, stale], total: 2, offset: 0, limit: 20, planning_revision: 4, snapshot: 'snapshot' }
const noop = () => undefined

describe('标记时间线', () => {
  it('同 UUID 的不同场景分别展示，失效、退场和截断均有文字说明', () => {
    const warn = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const html = renderToStaticMarkup(createElement(MarkerTimelineList, { page, editable: true, busy: false,
      canRelink: true, canDelete: true, onJump: noop, onRelink: noop, onDelete: noop }))
    expect(html).toContain('第 1 章 启程')
    expect(html).toContain('第 2 章 归来')
    expect(html).toContain('蓝色标记')
    expect(html).toContain('红色标记')
    expect(html).toContain('已失效')
    expect(html).toContain('来源场景已退场')
    expect(html).toContain('区块：旧备选')
    expect(html).toContain('原文片段过长，已截断')
    expect(html).toContain('重新关联')
    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })

  it('只读或尚未接入时不能伪装重关联和删除可用', () => {
    const html = renderToStaticMarkup(createElement(MarkerTimelineList, { page: { ...page, items: [stale] },
      editable: false, busy: false, canRelink: false, canDelete: false,
      onJump: noop, onRelink: noop, onDelete: noop }))
    expect(html).toMatch(/<button[^>]*disabled[^>]*>.*重新关联/)
    expect(html).toMatch(/<button[^>]*disabled[^>]*>.*删除标记/)
    expect(html).not.toMatch(/<button[^>]*>定位<\/button>/)
  })

  it('草稿末态与原有片段分别标注来源和各自的截断状态', () => {
    const item: MarkerTimelineItem = { ...stale, draft_excerpt: '删除前的新草稿', draft_excerpt_truncated: true,
      draft_last_start: 42, excerpt_truncated: true }
    const html = renderToStaticMarkup(createElement(MarkerTimelineList, { page: { ...page, items: [item] },
      editable: true, busy: false, canRelink: true, canDelete: true,
      onJump: noop, onRelink: noop, onDelete: noop }))
    expect(html).toContain('草稿末态片段')
    expect(html).toContain('服务端无法独立验证')
    expect(html).toContain('删除前的新草稿')
    expect(html).toContain('查看原有片段')
    expect(html).toContain('保留的旧片段')
    expect(html.match(/原文片段过长，已截断/g)).toHaveLength(2)
    expect(html).not.toMatch(/<button[^>]*>定位<\/button>/)
  })
})
