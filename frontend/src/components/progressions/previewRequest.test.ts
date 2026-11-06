import { afterEach, describe, expect, it, vi } from 'vitest'
import type { SettingEffective, SettingEntryFields } from '../../api/types'
import { loadEffectivePreview } from './preview'

const effective: SettingEffective = {
  timeline_id: 3, timeline_revision: 7, fields_revision: 2,
  position: { chapter_id: null, chapter_no: 5, boundary: 'start' },
  fields: { current_state: '受伤' }, custom_fields: {}, field_values: {}, applied_event_ids: [], orphaned_event_ids: [], baseline_status: 'confirmed',
}
const entryFields: SettingEntryFields = {
  entry: { reference: { kind: 'character', id: 4 }, name: '林远', type: 'character', category: null },
  timeline_id: 3, timeline_revision: 7, fields_revision: 2,
  ai_visibility_revision: 0,
  position: effective.position, fields: [], legacy_fields: [],
}
const input = () => ({ projectId: 1, timelineId: 3, timelineRevision: 7, fieldsRevision: 2, entry: entryFields.entry.reference,
  position: { anchor_kind: 'planned' as const, chapter_id: null, chapter_no: 5, boundary: 'start' as const }, signal: new AbortController().signal })

afterEach(() => vi.unstubAllGlobals())

describe('按章预览的完整读取与重试', () => {
  it('两个接口使用相同位置、信号和会话，并保留服务端的类型化投影', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (url) => new Response(JSON.stringify(String(url).includes('/effective') ? effective : entryFields)))
    vi.stubGlobal('fetch', fetch)
    const request = input()
    await expect(loadEffectivePreview(request)).resolves.toEqual({ data: effective, entryFields })
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/projects/1/setting-timelines/3/effective?chapter_no=5&boundary=start',
      '/api/v1/projects/1/setting-entries/character/4/fields?chapter_no=5&boundary=start',
    ])
    expect(fetch.mock.calls.every(([, init]) => init?.signal === request.signal && init.credentials === 'include')).toBe(true)
  })

  it('时间线相同而任一HTTP结果的目录版本不同，也不能拼成有效快照', async () => {
    vi.stubGlobal('fetch', async (url: string) => new Response(JSON.stringify(url.includes('/effective') ? { ...effective, fields_revision: 3 } : entryFields)))
    await expect(loadEffectivePreview(input())).rejects.toThrow('设定已更新')
    vi.stubGlobal('fetch', async (url: string) => new Response(JSON.stringify(url.includes('/effective') ? effective : { ...entryFields, fields_revision: 3 })))
    await expect(loadEffectivePreview(input())).rejects.toThrow('设定已更新')
  })

  it('读取失败后显式重试重新请求两个接口，不需要目录或条目版本发生改变', async () => {
    let failed = true
    const fetch = vi.fn(async (url: string) => failed && url.includes('/effective')
      ? new Response(JSON.stringify({ detail: '临时读取失败' }), { status: 503 })
      : new Response(JSON.stringify(url.includes('/effective') ? effective : entryFields)))
    vi.stubGlobal('fetch', fetch)
    await expect(loadEffectivePreview(input())).rejects.toMatchObject({ status: 503 })
    failed = false
    await expect(loadEffectivePreview(input())).resolves.toEqual({ data: effective, entryFields })
    expect(fetch).toHaveBeenCalledTimes(4)
  })
})
