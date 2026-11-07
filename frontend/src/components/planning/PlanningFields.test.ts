import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { EntrySelectProps } from '../entities/entrySelectState'
import { PlanningEntrySelect } from './PlanningFields'

const entrySelect = vi.hoisted(() => ({ props: undefined as EntrySelectProps<number> | undefined }))
vi.mock('../entities/EntrySelect', () => ({ default: (props: EntrySelectProps<number>) => { entrySelect.props = props; return null } }))

afterEach(() => {
  vi.unstubAllGlobals()
  entrySelect.props = undefined
})

describe('规划条目选择器适配', () => {
  it('向共享组件保留原多选值，分页请求透传类型、搜索和中止信号', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      revision: 9, total: 2, page: 2, page_size: 30,
      items: [{ reference: { kind: 'organization', id: 6 }, name: '青云宗', is_present: null }],
    })))
    vi.stubGlobal('fetch', fetcher)
    renderToStaticMarkup(createElement(PlanningEntrySelect, { projectId: 17, kind: 'organization', value: [8], label: '展示组织' }))
    expect(entrySelect.props).toMatchObject({ projectId: 17, value: [8], label: '展示组织' })
    const controller = new AbortController()
    const page = await entrySelect.props!.loadEntries({ q: '青云', page: 2, page_size: 30 }, controller.signal)
    const url = new URL(String(fetcher.mock.calls[0][0]), 'https://example.test')
    expect(url.pathname).toBe('/api/v1/projects/17/planning/entries')
    expect(Object.fromEntries(url.searchParams)).toEqual({ kind: 'organization', q: '青云', page: '2', page_size: '30' })
    expect(fetcher.mock.calls[0][1]).toMatchObject({ method: 'GET', signal: controller.signal, credentials: 'include' })
    expect(page).toEqual({ items: [{ value: 6, label: '青云宗' }], total: 2 })
    expect(entrySelect.props!.value).toEqual([8])
  })

  it('关系继续使用稳定时间线及历史标记，四类条目共享控件不接触该语义', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      revision: 5, total: 2, page: 1, page_size: 30,
      items: [
        { reference: { kind: 'relationship', timeline_id: 12 }, relationship_id: 99, name: '师徒', is_present: false },
        { reference: { kind: 'relationship', timeline_id: 13 }, relationship_id: 100, name: '同门', is_present: true },
      ],
    }))))
    const onChange = vi.fn()
    renderToStaticMarkup(createElement(PlanningEntrySelect, { projectId: 17, kind: 'relationship', value: [12], label: '展示关系', single: true, disabled: true, onChange }))
    expect(entrySelect.props).toMatchObject({ value: [12], single: true, disabled: true, onChange })
    expect(await entrySelect.props!.loadEntries({ q: '', page: 1, page_size: 30 }, new AbortController().signal)).toEqual({
      items: [{ value: 12, label: '师徒（历史关系）' }, { value: 13, label: '同门' }], total: 2,
    })
    expect(onChange).not.toHaveBeenCalled()
  })

  it.each(['character', 'world'] as const)('同名 %s 区分系列归属，但选择值仍是书内 ID', async (kind) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      revision: 10, total: 2, page: 1, page_size: 30,
      items: [
        { reference: { kind, id: 9 }, name: '重名', series_shared: false },
        { reference: { kind, id: 17 }, name: '重名', series_shared: true },
      ],
    }))))
    renderToStaticMarkup(createElement(PlanningEntrySelect, { projectId: 2, kind, label: '选择设定' }))
    expect(await entrySelect.props!.loadEntries({ q: '重名', page: 1, page_size: 30 }, new AbortController().signal)).toEqual({
      items: [{ value: 9, label: '重名' }, { value: 17, label: '重名（系列共享）' }], total: 2,
    })
  })
})
