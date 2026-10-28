import { afterEach, describe, expect, it, vi } from 'vitest'
import { agentApi, withAgentRefresh } from './agent'
import { ApiError } from './client'
import { readAgentChange } from './agentEvents'
import { subscribeTask, taskLink } from './tasks'
import { changeFixture } from '../utils/agent.test'

afterEach(() => vi.unstubAllGlobals())
describe('智能体 API 与事件边界', () => {
  it('会话设置保存在服务端，写操作走项目限定的路径', async () => {
    const calls: { url: string; init: RequestInit }[] = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => { calls.push({ url, init }); return new Response('{}') })
    await agentApi.list(3)
    await agentApi.create(3)
    await agentApi.update(3, 2, { auto_execute: true })
    await agentApi.send(3, 2, { content: '检查伏笔', page_context: { route: '/projects/3/chapters/7', chapter_id: 7, selection: '原文' } })
    await agentApi.change(3, 5, 'confirm')
    await agentApi.turn(3, 8, 'undo')
    expect(calls.map((c) => `${c.init.method} ${c.url}`)).toEqual(['GET /api/v1/projects/3/agent/conversations', 'POST /api/v1/projects/3/agent/conversations', 'PATCH /api/v1/projects/3/agent/conversations/2', 'POST /api/v1/projects/3/agent/conversations/2/messages', 'POST /api/v1/projects/3/agent/changes/5/confirm', 'POST /api/v1/projects/3/agent/turns/8/undo'])
    expect(JSON.parse(String(calls[2].init.body))).toEqual({ auto_execute: true })
    expect(JSON.parse(String(calls[3].init.body)).page_context.selection).toBe('原文')
    expect(taskLink({ kind: 'agent.turn', project_id: 3, chapter_id: null })).toBe('/projects/3/overview?agent=open')
  })
  it('409 先刷新卡片再报冲突，绝不自动重发确认', async () => {
    const conflict = new ApiError(409, '预览已变化，请重新确认', { code: 'preview_changed', change: changeFixture() })
    const operation = vi.fn().mockRejectedValue(conflict)
    const refresh = vi.fn().mockResolvedValue(undefined)
    await expect(withAgentRefresh(operation, refresh)).rejects.toBe(conflict)
    expect(operation).toHaveBeenCalledTimes(1)
    expect(refresh).toHaveBeenCalledTimes(1)
    await expect(withAgentRefresh(() => Promise.reject(new ApiError(400, '非法请求')), refresh)).rejects.toMatchObject({ status: 400 })
    expect(refresh).toHaveBeenCalledTimes(1)
  })
  it('流式工具开始/结束和提案执行按顺序交付，非法载荷不进入 UI', async () => {
    const proposal = changeFixture()
    const events: [string, unknown][] = [['tool_start', { call_id: 'x', name: 'get_character', label: '读取角色' }], ['tool_end', { call_id: 'x', ok: true, summary: '林远' }], ['proposal', { change: proposal }], ['proposal', { change: { id: 1 } }], ['executed', { change: { ...proposal, status: 'executed' } }], ['paused', { result: { reason: 'confirm' } }]]
    vi.stubGlobal('fetch', async () => new Response(events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join(''), { headers: { 'content-type': 'text/event-stream' } }))
    const log: string[] = []
    const end = await subscribeTask(4, { onToolStart: (e) => log.push(e.label), onToolEnd: (e) => log.push(e.summary), onProposal: (c) => log.push(c.status), onExecuted: (c) => log.push(c.status) })
    expect(log).toEqual(['读取角色', '林远', 'proposed', 'executed'])
    expect(end.status).toBe('paused')
    expect(readAgentChange({ change: { ...proposal, preview: { title: 'x', diff: [null], warnings: [] } } })).toBeNull()
  })
})
