import { describe, expect, it } from 'vitest'
import { categoryLocation, categoryQueryRequest, changeCategoryFilters, initialCategoryFilters, readCategoryLocation, withCategoryMetadata } from './categoryQuery'
import { entityDetailHref, readEntityFieldPosition } from '../../utils/entityLinks'

describe('分类查询位置、分页与读取版本', () => {
  it.each([
    { from: 'chapter', to: 'planned', boundary: 'start' },
    { from: 'chapter', to: 'planned', boundary: 'end' },
    { from: 'planned', to: 'chapter', boundary: 'start' },
    { from: 'planned', to: 'chapter', boundary: 'end' },
  ] as const)('$from → $to ($boundary) 的未填位置可往返URL，补齐后才查询', ({ from, to, boundary }) => {
    const { categoryId, filters } = readCategoryLocation(`?id=1&view=effective&${from === 'chapter' ? 'chapter_id=3' : 'chapter_no=21'}&boundary=${boundary}&page=3`)
    const original = structuredClone(filters)
    const empty = { anchor_kind: to, chapter_id: null, chapter_no: null, boundary }
    const switched = changeCategoryFilters(filters, { position: empty })
    const reopened = readCategoryLocation(categoryLocation(categoryId, switched))
    expect(reopened).toMatchObject({ categoryId: 1, filters: { page: 1, position: empty, invalidPosition: false } })
    expect(categoryQueryRequest(reopened.filters)).toMatchObject({ ok: false, error: '请选择有效章节' })
    const otherBoundary = boundary === 'start' ? 'end' : 'start'
    const changedBoundary = changeCategoryFilters(reopened.filters, { position: { ...empty, boundary: otherBoundary } })
    const restored = readCategoryLocation(categoryLocation(categoryId, changedBoundary)).filters
    expect(restored.position).toEqual({ ...empty, boundary: otherBoundary })
    const completed = changeCategoryFilters(restored, { position: { ...empty, boundary: otherBoundary, ...(to === 'chapter' ? { chapter_id: 3 } : { chapter_no: 21 }) } })
    expect(categoryQueryRequest(completed)).toMatchObject({ ok: true, query: { page: 1, boundary: otherBoundary, ...(to === 'chapter' ? { chapter_id: 3 } : { chapter_no: 21 }) } })
    expect(filters).toEqual(original)
    expect(categoryQueryRequest(filters)).toMatchObject({ ok: true, query: { page: 3, boundary, ...(from === 'chapter' ? { chapter_id: 3 } : { chapter_no: 21 }) } })
  })
  it('当前不带章节；更改筛选/时点/页大小回第一页，单独翻页保留条件', () => {
    const start = { ...initialCategoryFilters(), page: 4 }
    expect(categoryQueryRequest(start)).toMatchObject({ ok: true, position: null, query: { page: 4, page_size: 20 } })
    expect(changeCategoryFilters(start, { q: '林' }).page).toBe(1)
    expect(changeCategoryFilters(start, { types: ['character'] }).page).toBe(1)
    expect(changeCategoryFilters(start, { pageSize: 50 }).page).toBe(1)
    expect(changeCategoryFilters(start, { page: 5 }).page).toBe(5)
  })
  it('已有/计划章URL往返，四类详情使用同一位置及只读有效预览意图', () => {
    for (const position of [
      { anchor_kind: 'chapter' as const, chapter_id: 8, chapter_no: null, boundary: 'start' as const },
      { anchor_kind: 'planned' as const, chapter_id: null, chapter_no: 20, boundary: 'end' as const },
    ]) {
      const filters = { ...initialCategoryFilters(), position, types: ['character' as const], q: '同盟', page: 3 }
      expect(readCategoryLocation(categoryLocation(4, filters))).toEqual({ categoryId: 4, filters })
      const request = categoryQueryRequest(filters)
      if (!request.ok) throw new Error('valid position expected')
      for (const kind of ['character', 'world', 'organization', 'career'] as const) {
        const url = new URL(entityDetailHref(1, { kind, id: 10 }, request.position), 'https://example.test')
        expect(readEntityFieldPosition(url.searchParams)).toEqual(position)
      }
    }
  })
  it('伪整数、混合章锚、非法时点不能悄悄降级为当前', () => {
    for (const query of ['?id=4&view=effective&chapter_no=1e2&boundary=start', '?id=4&view=effective&chapter_id=1&chapter_no=2&boundary=end', '?id=4&view=effective&chapter_no=2&boundary=bad', '?id=4&view=effective&chapter_no=&chapter_id=&boundary=start', '?id=4&view=effective&chapter_no=&boundary=bad', '?id=4&view=effective&boundary=end']) {
      expect(categoryQueryRequest(readCategoryLocation(query).filters)).toMatchObject({ ok: false })
    }
  })
  it('元数据和分类/结果字段版本必须相同，拒绝拼合旧内容和新版本', () => {
    expect(() => withCategoryMetadata({ fields_revision: 3, categories_revision: 1 }, { items: [], total: 0, fields_revision: 4 })).toThrow('字段配置在读取期间发生变化')
    expect(withCategoryMetadata({ fields_revision: 3, categories_revision: 1 }, { items: [], total: 0, fields_revision: 3 })).toMatchObject({ value: { categories_revision: 1 }, definitions: { fields_revision: 3 } })
  })
})
