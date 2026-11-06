import { afterEach, describe, expect, it, vi } from 'vitest'
import { progressionApi } from './progressions'

afterEach(() => { vi.unstubAllGlobals() })

describe('设定变化 API', () => {
  it('共享事件PATCH保留省略与null差异，删除同时携带目录CAS', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ timeline_revision: 8, fields_revision: 5 }))))
    vi.stubGlobal('fetch', fetchMock)
    await progressionApi.updateProgression(4, 8, 18, { expected_revision: 7, expected_fields_revision: 5, note: '调整备注' })
    await progressionApi.updateProgression(4, 8, 18, { expected_revision: 8, expected_fields_revision: 5, value: null })
    await progressionApi.removeProgression(4, 8, 18, 9, 5)
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ expected_revision: 7, expected_fields_revision: 5, note: '调整备注' })
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({ expected_revision: 8, expected_fields_revision: 5, value: null })
    expect(fetchMock.mock.calls[2][0]).toBe('/api/v1/projects/4/setting-timelines/8/progressions/18?expected_revision=9&expected_fields_revision=5')
  })
  it('组织自身和职业定义时间线查询不携带人物身份', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ items: [], total: 0 }))))
    vi.stubGlobal('fetch', fetchMock)
    await progressionApi.findTimeline(4, { subject_kind: 'organization', organization_id: 7 })
    await progressionApi.findTimeline(4, { subject_kind: 'career_entry', career_id: 7 })
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual(['/api/v1/projects/4/setting-timelines?subject_kind=organization&organization_id=7', '/api/v1/projects/4/setting-timelines?subject_kind=career_entry&career_id=7'])
  })
  it('用作品和主体查询唯一时间线，读取请求可取消', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      items: [{ id: 8, subject_kind: 'world', revision: 2 }], total: 1,
    }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    expect(await progressionApi.findTimeline(4, { subject_kind: 'world', world_setting_id: 9 }, controller.signal))
      .toMatchObject({ id: 8, revision: 2 })
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/4/setting-timelines?subject_kind=world&world_setting_id=9')
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: 'include', signal: controller.signal })
  })

  it('事件增删使用扁平位置和期望修订号，不在客户端推断实际生效值', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ item: { id: 18 }, timeline_revision: 4 }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ timeline_revision: 5 }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const body = {
      expected_revision: 3, field_key: 'content', custom_field_id: null,
      operation: 'append' as const, value: '后来被毁', anchor_kind: 'chapter' as const,
      chapter_id: 20, chapter_no: null, boundary: 'end' as const, sort_order: 0, note: '',
    }

    expect(await progressionApi.createProgression(4, 8, body)).toMatchObject({ timeline_revision: 4 })
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/4/setting-timelines/8/progressions')
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'POST', body: JSON.stringify(body) })
    expect(await progressionApi.removeProgression(4, 8, 18, 4)).toEqual({ timeline_revision: 5 })
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/projects/4/setting-timelines/8/progressions/18?expected_revision=4')
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'DELETE' })
  })

  it('按章预览显式传章 ID 和章初／章末，使用独立设定接口', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      timeline_id: 8, position: { chapter_id: 12, chapter_no: 5, boundary: 'start' },
      fields: { content: '完整的剑' }, custom_fields: {}, applied_event_ids: [], orphaned_event_ids: [],
      baseline_status: 'confirmed', timeline_revision: 1,
    }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await progressionApi.effective(4, 8, 12, 'start')
    expect(result.fields.content).toBe('完整的剑')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/4/setting-timelines/8/effective?chapter_id=12&boundary=start')
  })

  it('确认旧基础资料时显式发送确认标记和已读取的修订号', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      timeline_id: 8, baseline: { content: '原设定' }, baseline_status: 'confirmed',
      legacy_compat_allowed: false, timeline_revision: 4,
    }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await progressionApi.updateBaseline(4, 8, { expected_revision: 3, changes: {}, confirm: true })
    expect(result.baseline_status).toBe('confirmed')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/4/setting-timelines/8/baseline')
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: 'PATCH', body: JSON.stringify({ expected_revision: 3, changes: {}, confirm: true }),
    })
  })

  it('世界观正文和条目信息以同一请求及版本保存', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 9, title: '灵剑', content: '最新投影' }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 9, title: '古剑', content: '最新投影' }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()
    await progressionApi.getWorldSetting(4, 9, controller.signal)
    const patch = { title: '古剑', content: '完整的剑', edit_mode: 'baseline' as const, expected_revision: 3 }
    await progressionApi.updateWorldBaseline(4, 9, patch)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/4/world/9')
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ signal: controller.signal })
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/projects/4/world/9')
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'PATCH', body: JSON.stringify(patch) })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('比较冲突时读取指定事件的最新版本，事件即便已停用仍可识别', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      item: { id: 18, revision: 6, active: false }, timeline_revision: 9,
    }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const result = await progressionApi.getProgression(4, 8, 18)
    expect(result.item.active).toBe(false)
    expect(result.timeline_revision).toBe(9)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/4/setting-timelines/8/progressions/18')
  })

  it('查询角色的全部职业时间线，而非只取第一条', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      items: [{ id: 8 }, { id: 9 }], total: 2,
    }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const result = await progressionApi.listTimelines(4, { subject_kind: 'career', character_id: 6 })
    expect(result.items).toHaveLength(2)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/4/setting-timelines?subject_kind=career&character_id=6')
  })

  it('计划章节按章节号读取有效值，并排除章节 ID', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      timeline_id: 8, position: { chapter_id: null, chapter_no: 12, boundary: 'end' }, fields: {},
      custom_fields: {}, applied_event_ids: [], orphaned_event_ids: [], baseline_status: 'confirmed', timeline_revision: 1,
    }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const result = await progressionApi.effectiveAtPosition(4, 8, {
      anchor_kind: 'planned', chapter_id: null, chapter_no: 12, boundary: 'end',
    })
    expect(result.position.chapter_no).toBe(12)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/4/setting-timelines/8/effective?chapter_no=12&boundary=end')
    expect(() => progressionApi.effectiveAtPosition(4, 8, {
      anchor_kind: 'planned', chapter_id: null, chapter_no: null, boundary: 'start',
    })).toThrow('请选择有效章节')
  })

  it('章节设定块使用独立的上下文预览接口', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      position: { chapter_id: 12, chapter_no: 3, boundary: 'start' }, text: '设定', legacy_unverified_count: 1, warnings: [],
    }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const result = await progressionApi.chapterSettingContext(4, 12)
    expect(result.legacy_unverified_count).toBe(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/4/chapters/12/setting-context')
  })
})
