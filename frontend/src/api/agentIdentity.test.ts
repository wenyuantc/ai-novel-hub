import { afterEach, describe, expect, it, vi } from 'vitest'
import { agentApi, withAgentRefresh } from './agent'
import { ApiError } from './client'
import { subscribeTask } from './tasks'
import { changeFixture } from '../utils/agent.test'

const CHANGE_KEY = '00000000-0000-4000-8000-000000000001'
const NEW_CHANGE_KEY = '00000000-0000-4000-8000-000000000002'
const TASK_KEY = '00000000-0000-4000-8000-000000000003'
const NEW_TASK_KEY = '00000000-0000-4000-8000-000000000004'
const version = 'a'.repeat(64)
const oldChange = () => changeFixture({ id: 5, change_key: CHANGE_KEY, view_version: version, turn_task_id: 8, turn_task_key: TASK_KEY })

afterEach(() => vi.unstubAllGlobals())

describe('智能体提案与任务的不可复用身份', () => {
  it('提交展示时的 change_key 与 view_version；ID 复用或预览变化 409 只刷新不重发', async () => {
    const fetch = vi.fn(async () => new Response('{"detail":"预览已变化"}', { status: 409 }))
    vi.stubGlobal('fetch', fetch)
    const shown = oldChange()
    const replacement = { ...shown, change_key: NEW_CHANGE_KEY, view_version: 'b'.repeat(64) }
    const refresh = vi.fn(async () => { void replacement })
    await expect(withAgentRefresh(() => agentApi.change(3, shown, 'confirm'), refresh)).rejects.toMatchObject({ status: 409 })
    expect(fetch).toHaveBeenCalledTimes(1)
    const [url, options] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/v1/projects/3/agent/changes/5/confirm')
    expect(JSON.parse(String(options.body))).toEqual({ change_key: CHANGE_KEY, view_version: version })
    expect(refresh).toHaveBeenCalledOnce()
  })

  it('缺少提案身份或预览版本时本地拒发；422 同样只刷新不重放', async () => {
    const fetch = vi.fn(async () => new Response('{}'))
    vi.stubGlobal('fetch', fetch)
    await expect(agentApi.change(3, { ...oldChange(), change_key: null }, 'confirm')).rejects.toThrow()
    await expect(agentApi.change(3, { ...oldChange(), view_version: '' }, 'undo')).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
    const refresh = vi.fn(async () => {})
    const action = vi.fn(async () => { throw new ApiError(422, '身份无效') })
    await expect(withAgentRefresh(action, refresh)).rejects.toMatchObject({ status: 422 })
    expect(action).toHaveBeenCalledOnce()
    expect(refresh).toHaveBeenCalledOnce()
  })

  it('请求发出后调用方对象发生变化，仍使用已确认的原身份处理结果', async () => {
    let resolve!: (response: Response) => void
    const fetch = vi.fn(() => new Promise<Response>((done) => { resolve = done }))
    vi.stubGlobal('fetch', fetch)
    const shown = oldChange()
    const expected = { ...shown, status: 'executed' as const }
    const request = agentApi.change(3, shown, 'confirm')
    shown.change_key = NEW_CHANGE_KEY
    shown.view_version = 'b'.repeat(64)
    shown.turn_task_key = NEW_TASK_KEY
    resolve(new Response(JSON.stringify(expected)))
    await expect(request).resolves.toMatchObject({ change_key: CHANGE_KEY, view_version: version, turn_task_key: TASK_KEY })
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({ change_key: CHANGE_KEY, view_version: version })
    expect(fetch).toHaveBeenCalledOnce()
  })

  it('整轮仅携带实际展示且符合动作的提案快照，集合变化由后端拒绝后不重发', async () => {
    const fetch = vi.fn(async () => new Response('{"detail":"集合变化"}', { status: 409 }))
    vi.stubGlobal('fetch', fetch)
    const shown = [oldChange(), changeFixture({ id: 6, change_key: NEW_CHANGE_KEY, view_version: 'b'.repeat(64),
      turn_task_id: 8, turn_task_key: TASK_KEY, status: 'proposed' }),
    changeFixture({ id: 7, change_key: '00000000-0000-4000-8000-000000000005', view_version: 'c'.repeat(64),
      turn_task_id: 8, turn_task_key: TASK_KEY, status: 'executed' })]
    const refresh = vi.fn(async () => {})
    await expect(withAgentRefresh(() => agentApi.turn(3, { id: 8, task_key: TASK_KEY }, 'confirm-all', shown), refresh))
      .rejects.toMatchObject({ status: 409 })
    const [, options] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(JSON.parse(String(options.body))).toEqual({ task_key: TASK_KEY, changes: [
      { id: 5, change_key: CHANGE_KEY, view_version: version },
      { id: 6, change_key: NEW_CHANGE_KEY, view_version: 'b'.repeat(64) },
    ] })
    expect(fetch).toHaveBeenCalledOnce()
    expect(refresh).toHaveBeenCalledOnce()
  })

  it('整轮撤销排除任务和外部调用；任一可操作项缺身份则不发请求', async () => {
    const fetch = vi.fn(async () => new Response('{"changes":[],"task":null}'))
    vi.stubGlobal('fetch', fetch)
    const shown = [oldChange(), { ...oldChange(), id: 6, status: 'executed' as const },
      { ...oldChange(), id: 7, status: 'executed' as const, kind: 'external' as const },
      { ...oldChange(), id: 8, status: 'executed' as const, kind: 'task' as const }]
    await agentApi.turn(3, { id: 8, task_key: TASK_KEY }, 'undo', shown)
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({ task_key: TASK_KEY, changes: [
      { id: 6, change_key: CHANGE_KEY, view_version: version },
    ] })
    await expect(agentApi.turn(3, { id: 8, task_key: null }, 'undo', shown)).rejects.toThrow('身份')
    await expect(agentApi.turn(3, { id: 8, task_key: TASK_KEY }, 'undo', [{ ...shown[1], view_version: null }])).rejects.toThrow('身份')
    expect(fetch).toHaveBeenCalledOnce()
  })

  it('任务无身份不订阅；断流重连始终携带最初 task_key，不采用复用 ID 的新身份', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(`event: snapshot\ndata: {"task":{"id":8,"task_key":"${TASK_KEY}","status":"running"},"text":"旧输出"}\n\n`, { headers: { 'content-type': 'text/event-stream' } }))
      .mockResolvedValueOnce(new Response(`event: snapshot\ndata: {"task":{"id":8,"task_key":"${NEW_TASK_KEY}","status":"running"},"text":"错误输出"}\n\n`, { headers: { 'content-type': 'text/event-stream' } }))
    vi.stubGlobal('fetch', fetch)
    await expect(subscribeTask({ id: 8, task_key: null }, {})).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
    const delivered = vi.fn()
    const original = { id: 8, task_key: TASK_KEY }
    await expect(subscribeTask(original, { onSnapshot: delivered, onReconnect: () => { original.task_key = NEW_TASK_KEY } }, undefined,
      { maxRetries: 1, sleep: async () => {} })).rejects.toThrow()
    const calls = fetch.mock.calls as Array<[string, RequestInit]>
    expect(calls.map(([url]) => new URL(url, 'https://local.test').searchParams.get('task_key'))).toEqual([TASK_KEY, TASK_KEY])
    expect(delivered).toHaveBeenCalledTimes(1)
    expect(delivered.mock.calls[0][1]).toBe('旧输出')
  })
})
