import { describe, expect, it, vi, afterEach } from 'vitest'
import type { AgentChange, AgentMessage } from '../api/types'
import { buildAgentTimeline, canUndoChange, changedResources, changeTaskId, externalCall, externalResult, mergeToolStep, toolLabel, upsertAgentChange } from './agent'
import { clampAgentWidth, readAgentPreferences, writeAgentPreferences } from './agentPreferences'
import { invalidateProject, subscribeProjectRefresh } from './projectRefresh'

export const changeFixture = (over: Partial<AgentChange> = {}): AgentChange => ({
  id: 1, conversation_id: 2, project_id: 3, turn_task_id: 4, tool_call_id: 'call_1', tool_name: 'update_character',
  arguments: { id: 10 }, kind: 'small', preview: { title: '修改林远', target: { id: 10 }, diff: [{ field: 'personality', label: '性格', before: '急躁', after: '冷静' }], warnings: [] },
  status: 'proposed', result: null, effects: [], error: '', executed_at: null, undone_at: null, seq: 1, created_at: '', updated_at: '', ...over,
})
const messageFixture = (over: Partial<AgentMessage>): AgentMessage => ({
  id: 1, conversation_id: 2, turn_task_id: 4, role: 'assistant', content: '', tool_calls: null, tool_call_id: null, tool_name: null, page_context: null, usage: null, created_at: '', updated_at: '', ...over,
})
afterEach(() => vi.unstubAllGlobals())

describe('智能体时间线与变更', () => {
  it('恢复后的工具历史与实时事件合并，提案在对应工具下只显示一次', () => {
    const messages = [messageFixture({ content: '准备修改', tool_calls: [{ id: 'call_1', name: 'update_character', arguments: '{}' }] }), messageFixture({ id: 2, role: 'tool', content: '已修改', tool_call_id: 'call_1', tool_name: 'update_character' })]
    let steps = mergeToolStep([], { call_id: 'call_1', name: 'update_character', label: '修改角色', turn_task_id: 4 })
    steps = mergeToolStep(steps, { call_id: 'call_1', ok: true, summary: '林远已更新', turn_task_id: 4 })
    const timeline = buildAgentTimeline(messages, [changeFixture({ status: 'executed' })], steps)
    expect(timeline.map((i) => i.type)).toEqual(['message', 'tool', 'change'])
    expect(timeline[1]).toMatchObject({ step: { label: '修改角色', ok: true, summary: '林远已更新' } })
  })
  it('不同轮次重用工具 id 不会吞掉历史步骤', () => {
    const messages = [4, 5].map((turn_task_id) => messageFixture({ id: turn_task_id, turn_task_id, tool_calls: [{ id: 'call_1', name: 'get_character', arguments: '{}' }] }))
    expect(buildAgentTimeline(messages, [], []).filter((i) => i.type === 'tool')).toHaveLength(2)
  })
  it('重连 proposal/executed 用 id 更新卡片，不追加重复提案', () => {
    const proposed = changeFixture()
    const executed = { ...proposed, status: 'executed' as const }
    expect(upsertAgentChange([proposed], executed)).toEqual([executed])
  })
  it('只有已执行的非任务修改可以撤销；任务提取 id 以查看或取消', () => {
    for (const status of ['proposed', 'executing', 'rejected', 'failed', 'undone'] as const) expect(canUndoChange(changeFixture({ status }))).toBe(false)
    expect(canUndoChange(changeFixture({ status: 'executed' }))).toBe(true)
    const task = changeFixture({ status: 'executed', kind: 'task', result: { task_id: 9 } })
    expect(canUndoChange(task)).toBe(false)
    expect(changeTaskId(task)).toBe(9)
    expect(changeTaskId(changeFixture())).toBeNull()
  })
  it('外部调用（external）不可撤销；解析插件 / 工具 / 参数与结果；MCP 与 Skill 工具名有中文标签', () => {
    const ext = changeFixture({ kind: 'external', status: 'executed', arguments: { q: 1 }, result: { text: '结果' }, preview: { title: 't', target: 'p', diff: [], warnings: [], external: { plugin: { id: 5, slug: 'dict', name: '词典' }, tool: 'lookup', arguments: { q: 2 }, read_only_hint: null } } })
    expect(canUndoChange(ext)).toBe(false)
    expect(externalCall(ext)).toEqual({ plugin: { id: 5, slug: 'dict', name: '词典' }, tool: 'lookup', arguments: { q: 2 }, read_only_hint: null })
    expect(externalCall(changeFixture())).toBeNull()
    expect(externalResult(ext)).toEqual({ text: '结果', error: false })
    expect(externalResult({ ...ext, result: { error: '超时' } })).toEqual({ text: '超时', error: true })
    expect(toolLabel('mcp__dict__look_up')).toBe('外部插件 dict · look_up')
    expect(toolLabel('load_skill')).toBe('加载 Skill')
  })
  it('级联 effects 使相关资源刷新；撤销正文同步章节与记忆', () => {
    expect(changedResources([changeFixture({ tool_name: 'delete_character', effects: [{ table: 'relationships', pk: 1, before: {}, after: null }, { table: 'organization_members', pk: 2, before: {}, after: null }] })])).toEqual(expect.arrayContaining(['project', 'characters', 'relationships', 'organizations']))
    expect(changedResources([changeFixture({ tool_name: 'replace_chapter_text' })])).toEqual(expect.arrayContaining(['chapters', 'memories']))
    const listener = vi.fn()
    const stop = subscribeProjectRefresh(listener)
    invalidateProject(3, ['characters'])
    stop()
    invalidateProject(4, ['chapters'])
    expect(listener).toHaveBeenCalledExactlyOnceWith(3, ['characters'])
  })
})

describe('智能体面板设备偏好', () => {
  it('拖宽及非法存储始终收敛至 320–720', () => {
    expect([100, 320, 450, 900, NaN].map(clampAgentWidth)).toEqual([320, 320, 450, 720, 400])
    vi.stubGlobal('localStorage', { getItem: () => '{"open":true,"width":10000}', setItem: vi.fn() })
    expect(readAgentPreferences()).toEqual({ open: true, width: 720 })
  })
  it('损坏或禁用存储时正常工作', () => {
    vi.stubGlobal('localStorage', { getItem: () => '{', setItem: () => { throw new Error('denied') } })
    expect(readAgentPreferences()).toEqual({ open: false, width: 400 })
    expect(() => writeAgentPreferences({ open: true, width: 400 })).not.toThrow()
  })
})
