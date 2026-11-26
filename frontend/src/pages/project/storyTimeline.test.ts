import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { TimelineItem } from '../../api/storyTimeline'
import StoryTimelinePage from './StoryTimelinePage'

const access = vi.hoisted(() => ({ canRead: true, canEdit: false, canReadNow: () => true }))
const chapter: TimelineItem = { kind: 'chapter', chapter_id: 3, chapter_no: 1, chapter_title: '归来', scene_id: null,
  scene_sort: null, scene_title: null, active: true, explicit_date: null, effective_date: null, source: 'unknown',
  date_label: null, revision: 5 }
const date = { year: 3, month_id: 'mist', day: 3 }
const scene: TimelineItem = { ...chapter, kind: 'scene', chapter_id: 8, chapter_no: 3, chapter_title: '往昔',
  scene_id: 15, scene_sort: 0, scene_title: '出发', effective_date: date, source: 'inherited', date_label: '星历三年雾月三日' }
vi.mock('../../hooks/useProjectId', () => ({ useProjectId: () => 7 }))
vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => access }))
vi.mock('../../stores/project', () => ({ useProjectStore: () => 1 }))
vi.mock('../../hooks/useMentionQuery', () => ({ useMentionQuery: (_pid: number, key: string) => ({
  data: key === 'story-calendar' ? { era_name: '星历', months: [], revision: 5 } : { items: [scene, chapter], total: 31, revision: 5 },
  loading: false, reload: vi.fn(),
}) }))
const render = () => renderToStaticMarkup(createElement(StaticRouter, null, createElement(StoryTimelinePage)))
beforeEach(() => { access.canRead = true; access.canEdit = false })

describe('故事时间线工作台', () => {
  it('故事顺序分列未标记录，继承日期标明来源，跳转使用稳定章景 ID', () => {
    const html = render()
    expect(html).toContain('沿用本章 · 星历三年雾月三日')
    expect(html).toContain('href="/projects/7/chapters/8?scene_id=15"')
    expect(html.indexOf('出发')).toBeLessThan(html.indexOf('<h2>未标时间</h2>'))
    expect(html.indexOf('<h2>未标时间</h2>')).toBeLessThan(html.indexOf('归来'))
    expect(html).toContain('时间线分页')
    expect(html).not.toContain('编辑日期')
    expect(html).not.toContain('设置作品历法')
  })
  it('编辑者可设置作品历法，章、景都有单独的编辑入口', () => {
    access.canEdit = true
    const html = render()
    expect(html).toContain('设置作品历法')
    expect(html.match(/编辑日期/g)).toHaveLength(2)
    expect(html).toContain('章节顺序')
  })
  it('读取权限撤销后不展示章景与日期', () => {
    access.canRead = false
    const html = render()
    expect(html).toContain('没有读取此作品的权限')
    expect(html).not.toContain('星历')
    expect(html).not.toContain('出发')
  })
})
