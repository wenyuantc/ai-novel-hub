import { App } from 'antd'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { SeriesDetail } from '../api/series'
import SeriesPage from './SeriesPage'

const state = vi.hoisted(() => ({
  detail: undefined as SeriesDetail | undefined,
  error: null as string | null,
}))

vi.mock('../hooks/useLoader', () => ({
  useLoader: () => ({ data: state.detail, loading: false, error: state.error, reload: vi.fn() }),
}))
vi.mock('../components/series/useSeriesAction', () => ({
  useSeriesIdentity: () => 'session:1',
  currentSeriesIdentity: () => 'session:1',
  useSeriesAction: () => ({ busy: false, error: null, run: vi.fn(), clearError: vi.fn() }),
}))
vi.mock('../components/series/SeriesLibrary', () => ({ default: () => createElement('div', null, '共享库') }))
vi.mock('../components/collaboration/MemberPanel', () => ({
  default: () => createElement('div', null, '系列成员管理面板'),
}))

function render() {
  return renderToStaticMarkup(createElement(App, null,
    createElement(MemoryRouter, { initialEntries: ['/series/7'] },
      createElement(Routes, null, createElement(Route, { path: '/series/:sid', element: createElement(SeriesPage) })),
    ),
  ))
}

beforeEach(() => {
  state.detail = { id: 7, name: '云上', revision: 2, book_count: 1, entry_count: 0,
    is_owner: false, can_manage_collaborators: false, authorization_version: 'a'.repeat(64),
    books: [{ id: 3, title: '公开成员书', recycled: false }] }
  state.error = null
})

describe('系列详情角色', () => {
  it('设定协作者可浏览共享库，但不显示作者专属管理和成员操作', () => {
    const html = render()
    expect(html).toContain('设定协作者')
    expect(html).toContain('共享库')
    expect(html).not.toContain('重命名')
    expect(html).not.toContain('加入作品')
    expect(html).not.toContain('退出系列')
    expect(html).not.toContain('系列成员管理面板')
  })

  it('作者保留系列管理及成员面板', () => {
    state.detail = { ...state.detail!, is_owner: true, can_manage_collaborators: true, authorization_version: null }
    const html = render()
    expect(html).toContain('重命名')
    expect(html).toContain('加入作品')
    expect(html).toContain('退出系列')
    expect(html).toContain('系列成员管理面板')
  })

  it('有隐藏书时不能误称系列尚无成员作品', () => {
    state.detail = { ...state.detail!, books: [], book_count: 2 }
    expect(render()).toContain('没有可查看的成员作品')
  })

  it('读取失败时不展示旧系列或旧成员资料', () => {
    state.error = '读取失败'
    const html = render()
    expect(html).not.toContain('公开成员书')
    expect(html).not.toContain('共享库')
    expect(html).not.toContain('系列成员管理面板')
  })
})
