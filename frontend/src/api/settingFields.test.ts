import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { isSettingFieldsRevisionConflict, settingFieldsApi } from './settingFields'

afterEach(() => vi.unstubAllGlobals())

describe('类型化设定字段客户端', () => {
  it('首次值和事件由复合请求原子提交，null不被省略，按章查询不发送另一种章锚', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => { calls.push({ url, init }); return new Response('{}') })
    const signal = new AbortController().signal
    const entry = { kind: 'organization' as const, id: 12 }
    await settingFieldsApi.listEntries(7, { types: ['organization', 'career'], q: '青云', page: 2, page_size: 20 }, signal)
    await settingFieldsApi.getEntryFields(7, entry, { anchor_kind: 'planned', chapter_id: null, chapter_no: 5, boundary: 'start' }, signal)
    await settingFieldsApi.getEntryFields(7, entry, null, signal)
    await settingFieldsApi.updateBaseline(7, entry, 11, { expected_fields_revision: 3, expected_timeline_revision: 0, field_type: 'reference', value: null }, signal)
    await settingFieldsApi.createProgression(7, entry, 11, { expected_fields_revision: 3, expected_timeline_revision: 0, field_type: 'dropdown', value: { option_id: 18 }, operation: 'replace', anchor_kind: 'chapter', chapter_id: 25, chapter_no: null, boundary: 'end' }, signal)
    await settingFieldsApi.bindLegacyField(7, entry, 19, { expected_fields_revision: 3, expected_timeline_revision: 2, definition_id: 11 }, signal)
    expect(calls.map(({ url }) => url)).toEqual([
      '/api/v1/projects/7/setting-entries?types=organization&types=career&q=%E9%9D%92%E4%BA%91&page=2&page_size=20',
      '/api/v1/projects/7/setting-entries/organization/12/fields?chapter_no=5&boundary=start',
      '/api/v1/projects/7/setting-entries/organization/12/fields',
      '/api/v1/projects/7/setting-entries/organization/12/fields/11/baseline',
      '/api/v1/projects/7/setting-entries/organization/12/fields/11/progressions',
      '/api/v1/projects/7/setting-entries/organization/12/legacy-fields/19/binding',
    ])
    expect(JSON.parse(String(calls[3].init.body))).toEqual({ expected_fields_revision: 3, expected_timeline_revision: 0, field_type: 'reference', value: null })
    expect(JSON.parse(String(calls[4].init.body))).toMatchObject({ value: { option_id: 18 }, expected_timeline_revision: 0 })
    expect(calls.every(({ init }) => init.signal === signal)).toBe(true)
  })
  it('定义和选项请求保留稳定身份、目录版本与会话，支持重复适用类型查询', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return new Response('{"fields_revision":4}')
    })
    const signal = new AbortController().signal
    await settingFieldsApi.listDefinitions(7, { types: ['character', 'world.place'], include_archived: true }, signal)
    await settingFieldsApi.getDefinition(7, 11, signal)
    await settingFieldsApi.createDefinition(7, { expected_fields_revision: 0, label: '境界', field_type: 'dropdown', applicable_types: ['character'] }, signal)
    await settingFieldsApi.updateDefinition(7, 11, { expected_fields_revision: 1, archived: false, sort: 0 }, signal)
    await settingFieldsApi.createOption(7, 11, { expected_fields_revision: 2, label: '筑基' }, signal)
    await settingFieldsApi.updateOption(7, 11, 15, { expected_fields_revision: 3, archived: true }, signal)
    await settingFieldsApi.removeOption(7, 11, 15, 4, signal)
    await settingFieldsApi.removeDefinition(7, 11, 5, signal)
    expect(calls.map(({ url }) => url)).toEqual([
      '/api/v1/projects/7/setting-field-definitions?types=character&types=world.place&include_archived=true',
      '/api/v1/projects/7/setting-field-definitions/11',
      '/api/v1/projects/7/setting-field-definitions',
      '/api/v1/projects/7/setting-field-definitions/11',
      '/api/v1/projects/7/setting-field-definitions/11/options',
      '/api/v1/projects/7/setting-field-definitions/11/options/15',
      '/api/v1/projects/7/setting-field-definitions/11/options/15?expected_fields_revision=4',
      '/api/v1/projects/7/setting-field-definitions/11?expected_fields_revision=5',
    ])
    expect(calls.every(({ init }) => init.credentials === 'include' && init.signal === signal)).toBe(true)
    expect(calls.map(({ init }) => init.method)).toEqual(['GET', 'GET', 'POST', 'PATCH', 'POST', 'PATCH', 'DELETE', 'DELETE'])
    expect(JSON.parse(String(calls[3].init.body))).toEqual({ expected_fields_revision: 1, archived: false, sort: 0 })
    expect(calls[6].init.body).toBeUndefined()
  })

  it('只识别字段目录版本冲突；依赖冲突保留业务含义且不重发', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ detail: '选项仍有历史引用', code: 'option_in_use' }), { status: 409 }))
    vi.stubGlobal('fetch', fetch)
    await expect(settingFieldsApi.removeOption(7, 11, 15, 2)).rejects.toMatchObject({ status: 409, body: { code: 'option_in_use' } })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(isSettingFieldsRevisionConflict(new ApiError(409, '目录已更新', { code: 'stale_revision', scope: 'fields', current_revision: 3 }))).toBe(true)
    expect(isSettingFieldsRevisionConflict(new ApiError(409, '时间线已更新', { code: 'stale_revision', scope: 'timeline' }))).toBe(false)
    expect(isSettingFieldsRevisionConflict(new ApiError(409, '仍被引用', { code: 'field_in_use' }))).toBe(false)
  })
})
