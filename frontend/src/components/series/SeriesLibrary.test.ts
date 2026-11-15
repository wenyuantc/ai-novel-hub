import { App } from 'antd'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { SeriesEntry } from '../../api/series'
import SeriesLibrary from './SeriesLibrary'

const state = vi.hoisted(() => ({
  entries: [] as SeriesEntry[],
  error: null as string | null,
}))

vi.mock('./useSeriesLoader', () => ({
  useSeriesLoader: () => ({
    data: { key: '7:2:::1:', data: { items: state.entries, total: state.entries.length, revision: 2 } },
    loading: false, error: state.error, reload: vi.fn(),
  }),
}))

function render(isOwner: boolean) {
  return renderToStaticMarkup(createElement(App, null, createElement(MemoryRouter, null,
    createElement(SeriesLibrary, { seriesId: 7, seriesRevision: 2, isOwner, books: [] }),
  )))
}

function deleteButton(html: string): string {
  return html.match(/<button\b[^>]*>.*?<\/button>/g)?.find((button) => /删\s*除<\/span>/.test(button)) ?? ''
}

function renderBook(canUseBook: boolean) {
  return renderToStaticMarkup(createElement(App, null, createElement(MemoryRouter, null,
    createElement(SeriesLibrary, { seriesId: 7, seriesRevision: 2, book: { id: 2, title: '本书' },
      canUseBook, canUseBookNow: () => canUseBook }),
  )))
}

beforeEach(() => {
  state.error = null
  state.entries = [{
    id: 12, kind: 'world', data: { title: '白塔', aliases: [], category: 'place', content: '共享档案' },
    ai_settings: { ai_mode: 'detected', mention_enabled: true, mention_case_sensitive: true,
      mention_exclusions: [], ai_hidden_builtin_fields: [] },
    has_uses: true, uses: [],
  }]
})

describe('共享设定删除', () => {
  it('设定协作者看不到绑定书时仍禁用删除并说明原因', () => {
    const html = render(false)
    expect(html).toContain('已有作品采用')
    expect(html).toContain('已被作品采用，请系列作者处理删除')
    expect(deleteButton(html)).toContain('disabled')
  })

  it('无任何绑定时允许协作者进入删除预检，作者也保留已有删除入口', () => {
    state.entries = [{ ...state.entries[0], has_uses: false }]
    const editorButton = deleteButton(render(false))
    expect(editorButton).not.toBe('')
    expect(editorButton).not.toContain('disabled')
    state.entries = [{ ...state.entries[0], has_uses: true }]
    const ownerButton = deleteButton(render(true))
    expect(ownerButton).not.toBe('')
    expect(ownerButton).not.toContain('disabled')
  })

  it('读取错误时不再渲染缓存的共享内容', () => {
    state.error = '无法读取系列'
    const html = render(false)
    expect(html).toContain('无法读取系列')
    expect(html).not.toContain('共享档案')
    expect(deleteButton(html)).toBe('')
  })

  it('本书只读或仅管理员旁路时不能采用，满足作品侧编辑身份才允许继续', () => {
    const findButton = (html: string) => html.match(/<button\b[^>]*>.*?<\/button>/g)
      ?.find((button) => button.includes('在本书使用')) ?? ''
    const readOnly = findButton(renderBook(false))
    expect(readOnly).not.toBe('')
    expect(readOnly).toContain('disabled')
    const editable = findButton(renderBook(true))
    expect(editable).not.toBe('')
    expect(editable).not.toContain('disabled')
  })
})
