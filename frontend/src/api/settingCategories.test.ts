import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, setUnauthorizedHandler } from './client'
import { settingCategoriesApi, settingCategoryConflictScope, settingCategoryRuleIndex, settingCategoryToInput } from './settingCategories'
import type { SettingCategory, SettingCategoryInput, SettingCategoryMutation, SettingTagsOut } from './types'

afterEach(() => { vi.unstubAllGlobals(); setUnauthorizedHandler(null) })

const draft: SettingCategoryInput = {
  name: '同行者', applicable_types: ['character', 'organization'], match_mode: 'all',
  rules: [{ kind: 'tag', tag: '同行', sort: 0 }, { kind: 'dropdown', definition_id: 5, option_id: 9, sort: 1 }],
}
function capture(response: unknown = {}) {
  const calls: Array<{ url: string; init: RequestInit }> = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => { calls.push({ url, init }); return new Response(JSON.stringify(response)) })
  return calls
}
const bodyOf = (call: { init: RequestInit }): unknown => JSON.parse(String(call.init.body))

describe('设定分类客户端合同', () => {
  it('现有分类转为编辑payload时去除规则行ID，仍保留下拉字段和选项身份且不修改响应', () => {
    const category: SettingCategory = {
      id: 11, project_id: 7, name: '同行者', applicable_types: ['character'], match_mode: 'all', sort: 2,
      rules: [{ id: 18, category_id: 11, kind: 'tag', tag: '同行', sort: 0 }, { id: 19, category_id: 11, kind: 'dropdown', definition_id: 5, option_id: 9, sort: 1 }],
    }
    const input = settingCategoryToInput(category)
    expect(input).toEqual({ name: '同行者', applicable_types: ['character'], match_mode: 'all', sort: 2,
      rules: [{ kind: 'tag', tag: '同行', sort: 0 }, { kind: 'dropdown', definition_id: 5, option_id: 9, sort: 1 }] })
    input.applicable_types.push('organization')
    input.rules[0].sort = 3
    expect(category.applicable_types).toEqual(['character'])
    expect(category.rules[0].sort).toBe(0)
  })
  it('列表和详情沿用认证请求，types重复编码，分页与两版本响应保留', async () => {
    const response = { items: [], total: 0, page: 2, page_size: 20, categories_revision: 3, fields_revision: 8 }
    const calls = capture(response)
    const signal = new AbortController().signal
    expect(await settingCategoriesApi.list(7, { types: ['character', 'world.place'], q: '青山', page: 2, page_size: 20 }, signal)).toEqual(response)
    await settingCategoriesApi.get(7, 11, signal)
    expect(calls.map(({ url }) => url)).toEqual([
      '/api/v1/projects/7/setting-categories?types=character&types=world.place&q=%E9%9D%92%E5%B1%B1&page=2&page_size=20',
      '/api/v1/projects/7/setting-categories/11',
    ])
    expect(calls.every(({ init }) => init.method === 'GET' && init.credentials === 'include' && init.signal === signal && init.body === undefined)).toBe(true)
  })

  it('创建和PATCH携带两个CAS，sort=0不丢失，省略字段不被补成null或默认值', async () => {
    const response: SettingCategoryMutation = { item: { name: draft.name, applicable_types: [...draft.applicable_types], match_mode: draft.match_mode, id: 11, project_id: 7, sort: 0,
      rules: [{ id: 18, category_id: 11, kind: 'tag', tag: '同行', sort: 0 }, { id: 19, category_id: 11, kind: 'dropdown', definition_id: 5, option_id: 9, sort: 1 }] }, categories_revision: 4, fields_revision: 8 }
    const calls = capture(response)
    const signal = new AbortController().signal
    const created = await settingCategoriesApi.create(7, { ...draft, expected_categories_revision: 3, expected_fields_revision: 8 }, signal)
    await settingCategoriesApi.update(7, 11, { sort: 0, expected_categories_revision: 4, expected_fields_revision: 8 }, signal)
    expect(created.categories_revision).toBe(4)
    expect(calls.map(({ init }) => init.method)).toEqual(['POST', 'PATCH'])
    expect(bodyOf(calls[0])).toEqual({ ...draft, expected_categories_revision: 3, expected_fields_revision: 8 })
    expect(bodyOf(calls[1])).toEqual({ sort: 0, expected_categories_revision: 4, expected_fields_revision: 8 })
    expect(calls.every(({ init }) => init.signal === signal)).toBe(true)
  })

  it('删除只提交分类CAS，成功响应保留实际两个版本', async () => {
    const versions = { categories_revision: 5, fields_revision: 12 }
    const calls = capture(versions)
    expect(await settingCategoriesApi.remove(7, 11, 4)).toEqual(versions)
    expect(calls[0].url).toBe('/api/v1/projects/7/setting-categories/11?expected_categories_revision=4')
    expect(calls[0].init).toMatchObject({ method: 'DELETE', credentials: 'include' })
    expect(calls[0].init.body).toBeUndefined()
  })

  it('成员查询保留服务端匹配理由，当前/已有章/计划章参数各自传输', async () => {
    const reason = { rule_index: 1, rule_id: 19, kind: 'dropdown', description: '境界为筑基', tag: null, definition_id: 5, definition_label: '境界', option_id: 9, option_label: '筑基' }
    const response = { category: { ...draft, sort: 0 }, categories_revision: 2, fields_revision: 3, position: null, items: [{ reference: { kind: 'character', id: 20 }, type: 'character', name: '林远', category: null, setting_tags: ['同行'], reasons: [reason] }], total: 1, page: 1, page_size: 50 }
    const calls = capture(response)
    const signal = new AbortController().signal
    expect(await settingCategoriesApi.entries(7, 11, {}, signal)).toEqual(response)
    const filters = { types: ['character' as const], chapter_id: 6, chapter_no: null, boundary: 'start' as const, page: 1, page_size: 50 }
    const original = structuredClone(filters)
    await settingCategoriesApi.entries(7, 11, filters, signal)
    await settingCategoriesApi.entries(7, 11, { chapter_no: 21, boundary: 'end' }, signal)
    expect(calls.map(({ url }) => url)).toEqual([
      '/api/v1/projects/7/setting-categories/11/entries',
      '/api/v1/projects/7/setting-categories/11/entries?types=character&chapter_id=6&boundary=start&page=1&page_size=50',
      '/api/v1/projects/7/setting-categories/11/entries?chapter_no=21&boundary=end',
    ])
    expect(filters).toEqual(original)
    expect(calls.every(({ init }) => init.signal === signal)).toBe(true)
  })

  it('只读preview传草稿和editing_category_id，不附写入CAS、不补默认filters', async () => {
    const calls = capture()
    const signal = new AbortController().signal
    await settingCategoriesApi.preview(7, { draft }, signal)
    await settingCategoriesApi.preview(7, { draft, filters: { chapter_no: 20, boundary: 'end', types: ['organization'], page: 2, page_size: 10 }, editing_category_id: 11 }, signal)
    await settingCategoriesApi.preview(7, { draft, editing_category_id: null }, signal)
    expect(calls.every(({ url, init }) => url === '/api/v1/projects/7/setting-categories/preview' && init.method === 'POST' && init.credentials === 'include' && init.signal === signal)).toBe(true)
    expect(bodyOf(calls[0])).toEqual({ draft })
    expect(bodyOf(calls[1])).toEqual({ draft, filters: { chapter_no: 20, boundary: 'end', types: ['organization'], page: 2, page_size: 10 }, editing_category_id: 11 })
    expect(bodyOf(calls[2])).toEqual({ draft, editing_category_id: null })
  })

  it('两种请求都在发出前拒绝双重章节位置，防止错误时点查询', () => {
    const calls = capture()
    expect(() => settingCategoriesApi.entries(7, 11, { chapter_id: 6, chapter_no: 20 })).toThrow('章节 ID 与计划章节号只能选择一个')
    expect(() => settingCategoriesApi.preview(7, { draft, filters: { chapter_id: 6, chapter_no: 20 } })).toThrow('章节 ID 与计划章节号只能选择一个')
    expect(calls).toHaveLength(0)
  })

  it('四类独立标签路径复用EntityRef，tag=[]明确清空且不混入字段版本', async () => {
    const response: SettingTagsOut = { entry: { reference: { kind: 'world', id: 20 }, type: 'world.place', name: '青山', category: 'place' }, setting_tags: [], categories_revision: 7 }
    const calls = capture(response)
    const signal = new AbortController().signal
    for (const kind of ['character', 'world', 'organization', 'career'] as const) await settingCategoriesApi.getTags(7, { kind, id: 20 }, signal)
    await settingCategoriesApi.updateTags(7, { kind: 'world', id: 20 }, { setting_tags: [], expected_categories_revision: 6 }, signal)
    expect(calls.map(({ url }) => url)).toEqual([
      '/api/v1/projects/7/setting-entries/character/20/tags', '/api/v1/projects/7/setting-entries/world/20/tags',
      '/api/v1/projects/7/setting-entries/organization/20/tags', '/api/v1/projects/7/setting-entries/career/20/tags',
      '/api/v1/projects/7/setting-entries/world/20/tags',
    ])
    expect(calls[4].init.method).toBe('PATCH')
    expect(bodyOf(calls[4])).toEqual({ setting_tags: [], expected_categories_revision: 6 })
    expect(calls.every(({ init }) => init.signal === signal && init.credentials === 'include')).toBe(true)
  })

  it('建议只读取有界目录；省略参数采用服务端默认，保留has_more与版本', async () => {
    const response = { items: ['同行', '同门'], has_more: true, categories_revision: 9 }
    const calls = capture(response)
    expect(await settingCategoriesApi.suggestTags(7)).toEqual(response)
    await settingCategoriesApi.suggestTags(7, { q: '同', limit: 100 })
    expect(calls.map(({ url }) => url)).toEqual(['/api/v1/projects/7/setting-entry-tags', '/api/v1/projects/7/setting-entry-tags?q=%E5%90%8C&limit=100'])
  })

  it.each([
    [401, { detail: '会话失效' }], [403, { detail: '只读用户', code: 'permission_denied' }],
    [409, { detail: '分类版本变化', code: 'stale_revision', scope: 'categories', current_revision: 4 }],
    [409, { detail: '字段版本变化', code: 'stale_revision', scope: 'fields', current_revision: 9 }],
    [409, { detail: '新规则不可选择停用选项', code: 'category_rule_archived', rule_index: 1 }],
    [422, { detail: '规则不合法' }],
  ])('HTTP %s错误体原样保留，不刷新版本或自动重提', async (status, body) => {
    const fetch = vi.fn(async () => new Response(JSON.stringify(body), { status }))
    vi.stubGlobal('fetch', fetch)
    await expect(settingCategoriesApi.create(7, { ...draft, expected_categories_revision: 3, expected_fields_revision: 8 })).rejects.toMatchObject({ status, detail: body.detail, body })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('版本冲突只认明确scope，规则错误索引独立收窄且不把业务409当版本冲突', () => {
    expect(settingCategoryConflictScope(new ApiError(409, '已更新', { code: 'stale_revision', scope: 'categories' }))).toBe('categories')
    expect(settingCategoryConflictScope(new ApiError(409, '已更新', { code: 'stale_revision', scope: 'fields' }))).toBe('fields')
    expect(settingCategoryConflictScope(new ApiError(409, '时间线更新', { code: 'stale_revision', scope: 'timeline' }))).toBeNull()
    expect(settingCategoryConflictScope(new ApiError(409, '同名', { code: 'category_conflict' }))).toBeNull()
    expect(settingCategoryConflictScope(new ApiError(400, '非版本冲突', { code: 'stale_revision', scope: 'categories' }))).toBeNull()
    expect(settingCategoryRuleIndex(new ApiError(409, '停用', { code: 'category_rule_archived', rule_index: 1 }))).toBe(1)
    for (const index of [-1, 20, '1', null, undefined]) expect(settingCategoryRuleIndex(new ApiError(400, '条件错误', { rule_index: index }))).toBeNull()
  })
})
