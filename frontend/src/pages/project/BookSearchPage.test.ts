import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from 'antd'
import BookSearchPage from './BookSearchPage'

const access = vi.hoisted(() => ({ ready: true, canRead: true, canEdit: false, canReadNow: () => true, canEditNow: () => false }))
vi.mock('../../hooks/useProjectId', () => ({ useProjectId: () => 7 }))
vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => access }))
vi.mock('../../stores/project', () => ({ useProjectStore: () => 1 }))
vi.mock('../../stores/auth', () => ({
  useAuthStore: (select: (state: unknown) => unknown) => select({
    status: 'authenticated', user: { id: 4, created_at: '2026-11-18', effective_permissions: [], roles: [] },
  }),
}))

const render = () => renderToStaticMarkup(createElement(StaticRouter, null, createElement(App, null, createElement(BookSearchPage))))

beforeEach(() => { access.canRead = true; access.canEdit = false })

describe('全书搜索页面权限', () => {
  it('查看者可查找、限制章节和打开历史，并隐藏替换控件', () => {
    const html = render()
    expect(html).toContain('查找原文')
    expect(html).toContain('限定章节范围')
    expect(html).toContain('修订记录')
    expect(html).not.toContain('预览替换')
    expect(html).not.toContain('book-search-replacement')
  })
  it('编辑者可输入新文本并预览', () => {
    access.canEdit = true
    const html = render()
    expect(html).toContain('预览替换')
    expect(html).toContain('book-search-replacement')
  })
  it('失去内容读取权限时撤下搜索及历史', () => {
    access.canRead = false
    const html = render()
    expect(html).toContain('没有读取此作品的权限')
    expect(html).not.toContain('修订记录')
  })
})
