import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SerializationPage from './SerializationPage'

const access = vi.hoisted(() => ({ ready: true, canRead: true, canEdit: false, canExport: true, canReadNow: () => true }))
const summary = vi.hoisted(() => ({ daily_goal: 2 as number | null, stock_count: 9, support_days: 4 as number | null,
  remaining_chapters: 1 as number | null, days: [], revision: 1 }))
const empty = { total: 0, items: [], offset: 0, limit: 12 }

vi.mock('../../hooks/useProjectId', () => ({ useProjectId: () => 5 }))
vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => access }))
vi.mock('../../stores/project', () => ({ useProjectStore: () => 2 }))
vi.mock('../../stores/auth', () => ({ useAuthStore: () => ({ id: 18 }) }))
vi.mock('../../hooks/useMentionQuery', () => ({
  useMentionQuery: (_pid: number, key: string) => ({
    data: key.startsWith('overview:') ? summary : key.startsWith('agenda:') ? { planned: empty, first_published: empty } : empty,
    loading: false, reload: vi.fn(),
  }),
}))

const render = () => renderToStaticMarkup(createElement(StaticRouter, null, createElement(SerializationPage)))
beforeEach(() => {
  access.canRead = true
  access.canEdit = false
  Object.assign(summary, { daily_goal: 2, support_days: 4, remaining_chapters: 1 })
})

describe('连载与存稿工作台', () => {
  it('按完整天数与余章展示库存，查看者可以浏览日历和记录', () => {
    const html = render()
    for (const label of ['9</strong> 章', '4</strong> 天', '余 1 章', '发布日历', '当天清单', '发布记录']) expect(html).toContain(label)
    expect(html).not.toContain('设置日更目标</span>')
  })
  it('未设目标时保留库存，编辑者可进入目标设置', () => {
    Object.assign(summary, { daily_goal: null, support_days: null, remaining_chapters: null })
    access.canEdit = true
    const html = render()
    expect(html).toContain('未设置日更目标')
    expect(html).toContain('9</strong> 章')
    expect(html).toContain('>设置日更目标<')
  })
  it('失去读取权即撤下日历和发布记录', () => {
    access.canRead = false
    const html = render()
    expect(html).toContain('没有读取此作品的权限')
    expect(html).not.toContain('发布日历')
  })
})
