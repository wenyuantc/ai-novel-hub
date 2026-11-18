import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom/server'
import { describe, expect, it } from 'vitest'
import type { HitPage, SearchHit, SelectedHit } from '../../api/bookSearch'
import { ApiError } from '../../api/client'
import { studioNavigation } from '../StudioNavigation'
import HitList from './HitList'
import { OperationAlert } from './OperationAlert'
import { asApplication, changeSelection, describeError, hitKey } from './selection'

const first: SearchHit = {
  chapter_id: 11, chapter_no: 2, chapter_title: '故地', start: 3, end: 6,
  before: '𠮷从', after: '向东', revision: 9, content_hash: 'a'.repeat(64), scene_id: 1,
  unavailable_reason: null, warning: null, token: 'b'.repeat(64),
}
const skipped = { ...first, start: 7, end: 10, before: '引文说：' }
const second = { ...first, chapter_id: 12, chapter_no: 5, scene_id: 4, chapter_title: '城门' }
const blocked = { ...first, start: 13, end: 16, token: null, unavailable_reason: '跨场景命中' }
const mode = { query: '青石镇', replacement: '青石城', start_chapter_id: 11, end_chapter_id: 12 }
const flags = { onSelect: () => {}, onPage: () => {}, onClear: () => {}, disabled: false }
const result: HitPage = { items: [first, skipped, blocked], page: 1, page_size: 20, total: 22, snapshot: 'x' }

describe('全书搜索的勾选与展示', () => {
  it('勾选本页省略不可替换项，切页后排除引文不影响其他页', () => {
    const page1 = changeSelection(new Map(), result.items, true)
    const both = changeSelection(page1, [second], true)
    const selected = changeSelection(both, [skipped], false)
    expect([...selected.keys()]).toEqual([hitKey(first), hitKey(second)])
    const body = asApplication(mode, selected, 'id')
    expect(body).toMatchObject({ ...mode, request_id: 'id', hits: [
      { chapter_id: 11, chapter_no: 2, chapter_title: '故地', start: 3 },
      { chapter_id: 12, chapter_no: 5, chapter_title: '城门', start: 3 },
    ] })
    expect(body.hits[0]).not.toHaveProperty('before')
    expect(body.hits[0]).not.toHaveProperty('after')
    expect(page1.size).toBe(2)
  })

  it('空替换可删除，原样替换、空选区与普通查询不能构造提交', () => {
    const selected = changeSelection(new Map(), [first], true)
    expect(asApplication({ ...mode, replacement: '' }, selected, 'id').replacement).toBe('')
    expect(() => asApplication({ ...mode, replacement: mode.query }, selected, 'id')).toThrow()
    expect(() => asApplication({ query: mode.query }, selected, 'id')).toThrow()
    expect(() => asApplication(mode, new Map(), 'id')).toThrow()
  })

  it('预览展示原文、改文和受限原因，链接给出同段内每处位置，删除有可读标识', () => {
    const html = renderToStaticMarkup(createElement(StaticRouter, null, createElement(HitList, {
      ...flags, result, mode, selected: new Map<string, SelectedHit>(), projectId: 42,
    })))
    expect(html).toContain('𠮷从<mark>青石镇</mark>向东')
    expect(html).toContain('𠮷从<ins>青石城</ins>向东')
    expect(html).toContain('跨场景命中')
    expect(html).toContain('mention_start=3')
    expect(html).toContain('mention_start=7')
    expect(html).toContain('勾选本页')
    const deletion = renderToStaticMarkup(createElement(StaticRouter, null, createElement(HitList, {
      ...flags, result, mode: { ...mode, replacement: '' }, selected: new Map(), projectId: 42,
    })))
    expect(deletion).toContain('〔删除〕')
  })

  it('只读搜索在协作导航中可见，普通结果只有原文，保留空白与特殊字符', () => {
    expect(studioNavigation(42, false, false).flatMap((g) => g.items).find((item) => item.label === '全书搜索')?.to)
      .toBe('/projects/42/book-search')
    const html = renderToStaticMarkup(createElement(StaticRouter, null, createElement(HitList, {
      ...flags, result, mode: { query: '<标记> ' }, selected: new Map(), projectId: 42,
    })))
    expect(html).toContain('<mark>&lt;标记&gt; </mark>')
    expect(html).not.toContain('勾选')
    expect(html).not.toContain('替换后')
  })

  it('409 显示每个冲突章节，未知回执与明确冲突分别反馈', () => {
    const error = describeError(new ApiError(409, '整批撤销已停止', { conflicts: [
      { chapter_id: null, chapter_no: 2, chapter_title: '故地', reason: '已删除' },
      { chapter_id: 12, chapter_no: 5, chapter_title: '城门', reason: '内容已修改' },
    ] }))
    expect(error.unknown).toBe(false)
    const html = renderToStaticMarkup(createElement(OperationAlert, { error }))
    expect(html).toContain('第 2 章 · 故地 · 已删除')
    expect(html).toContain('第 5 章 · 城门 · 内容已修改')
    expect(describeError(new ApiError(0, '网络错误')).unknown).toBe(true)
  })
})
