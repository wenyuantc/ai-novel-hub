import { afterEach, describe, expect, it, vi } from 'vitest'
import { subscribeProjectRefresh } from '../utils/projectRefresh'
import { ApiError } from './client'
import { planningApi, readPlanningReviewResource } from './planning'

afterEach(() => vi.unstubAllGlobals())
function mockResponse(body: unknown = { revision: 9 }, status = 200) {
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(body), { status })))
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}
function url(fetcher: ReturnType<typeof mockResponse>, index = 0) { return new URL(String(fetcher.mock.calls[index][0]), 'https://example.test') }

describe('规划 API 请求契约', () => {
  it('矩阵精确目标与混合列保留类型，不把关系时间线当普通实体 id', async () => {
    const fetcher = mockResponse()
    const controller = new AbortController()
    const columns = [{ kind: 'setting' as const, reference: { kind: 'relationship' as const, timeline_id: 7 } }, { kind: 'subplot' as const, subplot_id: 7 }]
    const sources = [{ kind: 'plan' as const, id: 3 }, { kind: 'detached' as const, id: 4 }]
    await planningApi.matrix(5, { page: 2, page_size: 100, column_page: 3, column_mode: 'custom', column_refs: columns, source_refs: sources, volume_no: 2, tag_id: 8, q: '雨夜', entity_kind: 'world', entry_q: '北境' }, controller.signal)
    const requestUrl = url(fetcher)
    expect(requestUrl.pathname).toBe('/api/v1/projects/5/planning/matrix')
    expect(JSON.parse(requestUrl.searchParams.get('column_refs')!)).toEqual(columns)
    expect(JSON.parse(requestUrl.searchParams.get('source_refs')!)).toEqual(sources)
    expect(Object.fromEntries(requestUrl.searchParams)).toMatchObject({ page: '2', page_size: '100', column_page: '3', volume_no: '2', tag_id: '8', q: '雨夜', entity_kind: 'world', entry_q: '北境' })
    expect(fetcher.mock.calls[0][1]).toMatchObject({ method: 'GET', signal: controller.signal, credentials: 'include' })
  })
  it('交织两类顺序轴独立请求，支线筛选使用重复参数而非逗号拼接', async () => {
    const fetcher = mockResponse()
    await planningApi.weave(1, { axis: 'chapter', page: 2, page_size: 30, subplot_page: 3, subplot_page_size: 20, subplot_ids: [6, 9], gap_threshold: 4 })
    await planningApi.weave(1, { axis: 'plan', page: 1, gap_threshold: 4 })
    expect(url(fetcher).searchParams.getAll('subplot_ids')).toEqual(['6', '9'])
    expect(Object.fromEntries(url(fetcher).searchParams)).toMatchObject({ axis: 'chapter', page: '2', subplot_page: '3', gap_threshold: '4' })
    expect(url(fetcher, 1).searchParams.get('axis')).toBe('plan')
    expect(url(fetcher, 1).searchParams.has('subplot_ids')).toBe(false)
  })
  it('批量一次提交版本和全部目标，成功后刷新同作品规划与源视图', async () => {
    const fetcher = mockResponse({ revision: 11, rows: [] })
    const listener = vi.fn()
    const unsubscribe = subscribeProjectRefresh(listener)
    try {
      const body = { expected_revision: 10, targets: [{ kind: 'chapter' as const, id: 2 }, { kind: 'plan' as const, id: 2 }], references: [{ kind: 'relationship' as const, op: 'replace' as const, timeline_ids: [12] }], pov: { op: 'clear' as const } }
      expect(await planningApi.batch(3, body)).toEqual({ revision: 11, rows: [] })
      expect(fetcher).toHaveBeenCalledTimes(1)
      expect(fetcher.mock.calls[0][1]).toMatchObject({ method: 'PATCH', body: JSON.stringify(body) })
      expect(listener).toHaveBeenCalledWith(3, ['planning', 'chapters', 'outlines'])
    } finally { unsubscribe() }
  })
  it('删除传查询版本并读取 200 revision；节点按来源区分 plan 与 chapter', async () => {
    const fetcher = mockResponse({ revision: 12 })
    expect(await planningApi.deleteNode(2, 4, { kind: 'plan', id: 8 }, 11)).toEqual({ revision: 12 })
    await planningApi.deleteCrossing(2, 9, 12)
    expect(url(fetcher).pathname).toBe('/api/v1/projects/2/planning/subplots/4/nodes/plan/8')
    expect(url(fetcher).searchParams.get('expected_revision')).toBe('11')
    expect(fetcher.mock.calls[0][1]).toMatchObject({ method: 'DELETE', body: undefined })
    expect(url(fetcher, 1).searchParams.get('expected_revision')).toBe('12')
  })
  it('移动交汇发送完整参与集合，显式 merge 携带作者选择的文本', async () => {
    const fetcher = mockResponse()
    const crossing = { expected_revision: 5, source: { kind: 'chapter' as const, id: 15 }, title: '三线碰面', description: '共同发现线索', subplot_ids: [2, 5, 8] }
    const merge = { expected_revision: 6, survivor_unit_id: 3, other_unit_id: 4, pov_character_id: null, node_resolutions: [{ subplot_id: 2, keep_node_id: 18, content: '合并后的推进目标' }] }
    await planningApi.updateCrossing(1, 12, crossing)
    await planningApi.merge(1, merge)
    expect(fetcher.mock.calls[0][1]).toMatchObject({ method: 'PATCH', body: JSON.stringify(crossing) })
    expect(url(fetcher, 1).pathname).toBe('/api/v1/projects/1/planning/units/merge')
    expect(fetcher.mock.calls[1][1]).toMatchObject({ method: 'POST', body: JSON.stringify(merge) })
  })
  it('冲突保留结构化响应，网络失败也不会自动重放写请求或发成功通知', async () => {
    const listener = vi.fn()
    const unsubscribe = subscribeProjectRefresh(listener)
    try {
      const errorBody = { code: 'planning_stale_revision', detail: '规划已被另一页面更新', current_revision: 15 }
      const fetcher = mockResponse(errorBody, 409)
      await expect(planningApi.createTag(1, { expected_revision: 3, name: '伏笔' })).rejects.toMatchObject({ status: 409, body: errorBody })
      expect(fetcher).toHaveBeenCalledTimes(1)
      fetcher.mockRejectedValueOnce(new TypeError('network'))
      await expect(planningApi.createTag(1, { expected_revision: 3, name: '伏笔' })).rejects.toBeInstanceOf(ApiError)
      expect(fetcher).toHaveBeenCalledTimes(2)
      expect(listener).not.toHaveBeenCalled()
    } finally { unsubscribe() }
  })
  it('未建章预览仍用 plan 身份与只读请求，局部用途不改变生成目标', async () => {
    const fetcher = mockResponse()
    const controller = new AbortController()
    await planningApi.preview(3, { kind: 'plan', id: 30 }, 'selection', controller.signal)
    expect(Object.fromEntries(url(fetcher).searchParams)).toEqual({ source_kind: 'plan', source_id: '30', scope: 'selection' })
    expect(fetcher.mock.calls[0][1]).toMatchObject({ method: 'GET', signal: controller.signal, body: undefined })
  })
  it('冲突核对按稳定 ID 翻页读取改名条目，不用旧名称漏掉目标', async () => {
    const fetcher = mockResponse()
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ revision: 4, page: 1, page_size: 100, total: 101, items: [{ id: 2, name: '其他支线' }] })))
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ revision: 4, page: 2, page_size: 100, total: 101, items: [{ id: 8, name: '已改名支线', description: '最新构思' }] })))
    const controller = new AbortController()
    const result = await readPlanningReviewResource(3, { kind: 'subplots', id: 8, q: '旧名称' }, controller.signal)
    expect(result.items).toEqual([{ id: 8, name: '已改名支线', description: '最新构思' }])
    expect(url(fetcher).searchParams.has('q')).toBe(false)
    expect(url(fetcher, 1).searchParams.get('page')).toBe('2')
    expect(fetcher.mock.calls[1][1].signal).toBe(controller.signal)
  })

})
