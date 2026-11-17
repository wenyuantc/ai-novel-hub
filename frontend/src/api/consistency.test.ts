import { afterEach, describe, expect, it, vi } from 'vitest'
import { issueListQuery } from '../utils/consistency'
import { consistencyApi } from './consistency'
import { canStreamTask, TASK_KIND_LABELS, taskLink } from './tasks'

afterEach(() => vi.unstubAllGlobals())

function stubFetch() {
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response('{}')))
  vi.stubGlobal('fetch', fetcher)
  return {
    fetcher,
    url: (index: number) => new URL(String(fetcher.mock.calls[index][0]), 'https://example.test'),
    init: (index: number) => fetcher.mock.calls[index][1] as RequestInit,
  }
}

describe('一致性检查 API 合同', () => {
  it('预览与发起：路径、方法与请求体；全书检查不带起止章节', async () => {
    const { url, init } = stubFetch()
    const controller = new AbortController()
    await consistencyApi.preview(4, { mode: 'all' }, controller.signal)
    await consistencyApi.start(4, { mode: 'range', start_chapter_id: 3, end_chapter_id: 9, ai_config_id: null })
    expect(url(0).pathname).toBe('/api/v1/projects/4/consistency/checks/preview')
    expect(init(0)).toMatchObject({ method: 'POST', credentials: 'include', signal: controller.signal, body: JSON.stringify({ mode: 'all' }) })
    expect(url(1).pathname).toBe('/api/v1/projects/4/consistency/checks')
    expect(JSON.parse(String(init(1).body))).toEqual({ mode: 'range', start_chapter_id: 3, end_chapter_id: 9, ai_config_id: null })
  })

  it('问题列表：筛选参数序列化到查询串，未选条件不出现', async () => {
    const { url } = stubFetch()
    await consistencyApi.issues(2, issueListQuery({ status: 'open', category: 'item', chapterId: 8, recheck: 'no' }, 2))
    await consistencyApi.issues(2, issueListQuery({}, 1))
    expect(url(0).pathname).toBe('/api/v1/projects/2/consistency/issues')
    expect(Object.fromEntries(url(0).searchParams)).toEqual({
      status: 'open', category: 'item', chapter_id: '8', needs_recheck: 'false', page: '2', page_size: '20',
    })
    expect(Object.fromEntries(url(1).searchParams)).toEqual({ page: '1', page_size: '20' })
  })

  it('概要、详情、状态写入与重新核查', async () => {
    const { url, init } = stubFetch()
    await consistencyApi.summary(2)
    await consistencyApi.issue(2, 7)
    await consistencyApi.update(2, 7, { revision: 3, note: '' })
    await consistencyApi.recheck(2, 7)
    expect(url(0).pathname).toBe('/api/v1/projects/2/consistency/summary')
    expect(url(1).pathname).toBe('/api/v1/projects/2/consistency/issues/7')
    expect(init(2)).toMatchObject({ method: 'PATCH', body: JSON.stringify({ revision: 3, note: '' }) })
    expect(url(3).pathname).toBe('/api/v1/projects/2/consistency/issues/7/recheck')
    expect(init(3)).toMatchObject({ method: 'POST', body: JSON.stringify({ ai_config_id: null }) })
  })

  it('修改影响：概览、单章详情、评估与确认状态写入', async () => {
    const { url, init } = stubFetch()
    const controller = new AbortController()
    await consistencyApi.impacts(2, controller.signal)
    await consistencyApi.impact(2, 5)
    await consistencyApi.assess(2, 5)
    await consistencyApi.assess(2, 5, 9)
    await consistencyApi.updateImpact(2, 5, { revision: 0, acknowledged: [31] })
    await consistencyApi.updateImpact(2, 5, { revision: 4, items: [{ chapter_id: 12, status: 'reviewed' }] })
    expect(url(0).pathname).toBe('/api/v1/projects/2/consistency/impacts')
    expect(init(0)).toMatchObject({ signal: controller.signal })
    expect(url(1).pathname).toBe('/api/v1/projects/2/consistency/impacts/5')
    expect(url(2).pathname).toBe('/api/v1/projects/2/consistency/impacts/5/assess')
    expect(init(2)).toMatchObject({ method: 'POST', body: JSON.stringify({ ai_config_id: null }) })
    expect(init(3)).toMatchObject({ method: 'POST', body: JSON.stringify({ ai_config_id: 9 }) })
    expect(url(4).pathname).toBe('/api/v1/projects/2/consistency/impacts/5')
    expect(init(4)).toMatchObject({ method: 'PATCH', body: JSON.stringify({ revision: 0, acknowledged: [31] }) })
    expect(init(5)).toMatchObject({ method: 'PATCH', body: JSON.stringify({ revision: 4, items: [{ chapter_id: 12, status: 'reviewed' }] }) })
  })

  it('修改影响：评估进行中轮询的是只查任务的轻量接口，不是概览', async () => {
    const { url, init } = stubFetch()
    const controller = new AbortController()
    await consistencyApi.impactRunning(2, controller.signal)
    expect(url(0).pathname).toBe('/api/v1/projects/2/consistency/impacts/running')
    expect(init(0)).toMatchObject({ signal: controller.signal })
  })

  it('任务面板：修改影响评估的标签、跳转到影响页签（带任务 id，由页面定位章节）、协作者可订阅', () => {
    expect(TASK_KIND_LABELS['consistency.impact']).toBe('修改影响评估')
    expect(taskLink({ kind: 'consistency.impact', project_id: 2, chapter_id: null })).toBe('/projects/2/consistency?tab=impact')
    expect(taskLink({ kind: 'consistency.impact', project_id: 2, chapter_id: 5 })).toBe('/projects/2/consistency?tab=impact&chapter=5')
    // 评估任务本身不带章节：带上任务 id
    expect(taskLink({ id: 41, kind: 'consistency.impact', project_id: 2, chapter_id: null })).toBe('/projects/2/consistency?tab=impact&task=41')
    expect(taskLink({ kind: 'consistency.impact', project_id: null, chapter_id: 5 })).toBeNull()
    expect(canStreamTask('consistency.impact', true)).toBe(true)
  })

  it('任务面板：种类标签、跳转到检查页、协作者可订阅', () => {
    expect(TASK_KIND_LABELS['consistency.check']).toBe('一致性检查')
    expect(taskLink({ kind: 'consistency.check', project_id: 2, chapter_id: null })).toBe('/projects/2/consistency')
    expect(canStreamTask('consistency.check', true)).toBe(true)
  })
})
