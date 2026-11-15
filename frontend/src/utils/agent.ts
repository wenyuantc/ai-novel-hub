import type { AgentChange, AgentChangeStatus, AgentExternalCall, AgentMessage, AgentToolEnd, AgentToolStart, Task } from '../api/types'
import { canRetryTask, hasTaskIdentity, isOpenTask } from '../api/tasks'
import type { ProjectResource } from './projectRefresh'

export const AGENT_CHANGE_STATUS: Record<AgentChangeStatus, { label: string; color: string }> = {
  proposed: { label: '待确认', color: 'gold' }, executing: { label: '执行中', color: 'processing' },
  executed: { label: '已执行', color: 'success' }, rejected: { label: '已拒绝', color: 'default' },
  failed: { label: '执行失败', color: 'error' }, undone: { label: '已撤销', color: 'default' },
}
export interface AgentToolStep extends AgentToolStart { ok?: boolean; summary?: string; turn_task_id?: number | null; turn_task_key?: string | null }
export function mergeToolStep(steps: AgentToolStep[], event: (AgentToolStart | AgentToolEnd) & { turn_task_id?: number | null; turn_task_key?: string | null }): AgentToolStep[] {
  const old = steps.find((s) => s.call_id === event.call_id && s.turn_task_id === event.turn_task_id && s.turn_task_key === event.turn_task_key)
  const next = { name: '', label: '工具调用', ...old, ...event }
  return old ? steps.map((s) => s.call_id === event.call_id && s.turn_task_id === event.turn_task_id && s.turn_task_key === event.turn_task_key ? next : s) : [...steps, next]
}
export function upsertAgentChange(changes: AgentChange[], change: AgentChange): AgentChange[] {
  const current = changes.find((item) => item.id === change.id)
  if (current && (current.change_key !== change.change_key || current.view_version !== change.view_version)) return changes
  if (current && current.status !== change.status) {
    const next = current.status === 'proposed' ? ['executing', 'executed', 'rejected', 'failed']
      : current.status === 'executing' ? ['executed', 'failed'] : current.status === 'executed' ? ['undone'] : []
    if (!next.includes(change.status)) return changes
  }
  return [...changes.filter((c) => c.id !== change.id), change].sort((a, b) => a.seq - b.seq || a.id - b.id)
}
/** 任务只能取消；外部插件调用（external）无法撤销 */
export function canUndoChange(change: AgentChange): boolean { return change.status === 'executed' && change.kind !== 'task' && change.kind !== 'external' }
/** external 提案的插件、工具与参数（预览结构不完整时返回 null） */
export function externalCall(change: AgentChange): AgentExternalCall | null {
  const ext = change.kind === 'external' ? change.preview.external : undefined
  if (!ext || typeof ext !== 'object' || typeof ext.tool !== 'string' || typeof ext.plugin !== 'object' || ext.plugin === null) return null
  return { plugin: { id: Number(ext.plugin.id), slug: String(ext.plugin.slug ?? ''), name: String(ext.plugin.name ?? '') }, tool: ext.tool, arguments: ext.arguments && typeof ext.arguments === 'object' ? ext.arguments : change.arguments, read_only_hint: typeof ext.read_only_hint === 'boolean' ? ext.read_only_hint : null }
}
/** 外部调用执行后的文本结果（result.text）或错误 */
export function externalResult(change: AgentChange): { text: string; error: boolean } | null {
  const r = change.result
  if (!r) return null
  if (typeof r.error === 'string') return { text: r.error, error: true }
  if (typeof r.text === 'string') return { text: r.text, error: false }
  return null
}
export function agentValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '（空）'
  if (typeof value === 'string') return value
  if (typeof value === 'boolean') return value ? '是' : '否'
  return JSON.stringify(value, null, 2)
}
export function toolLabel(name: string): string {
  // MCP 工具：mcp__插件标识__工具名（实时事件里的 label 由服务端给出插件名）
  const mcp = /^mcp__([a-z0-9-]+)__(.+)$/.exec(name)
  if (mcp) return `外部插件 ${mcp[1]} · ${mcp[2]}`
  const direct: Record<string, string> = { get_project_overview: '读取项目概览', update_project: '修改项目', replace_chapter_text: '替换章节正文', update_chapter_meta: '修改章节标题', update_chapter_plans: '修改细纲', start_task: '启动后台任务', read_chapter: '阅读章节', get_outline_tree: '读取大纲树', search_memories: '检索剧情记忆', search_knowledge: '检索知识库', list_active_tasks: '查看进行中的任务', get_chapter_analysis: '读取章节分析', list_skills: '查看 Skill 列表', load_skill: '加载 Skill', read_skill_reference: '读取 Skill 参考资料', set_org_member: '设置组织成员', remove_org_member: '移除组织成员', assign_career: '设置角色职业', remove_career_assignment: '移除角色职业' }
  if (direct[name]) return direct[name]
  const verbs: Record<string, string> = { list: '查看', get: '读取', create: '新建', update: '修改', delete: '删除' }
  const nouns: Record<string, string> = { character: '角色', characters: '角色', organization: '组织', organizations: '组织', relationship: '关系', relationships: '关系', career: '职业', careers: '职业', world_setting: '世界观', world_settings: '世界观', outline: '大纲', chapter: '章节', chapters: '章节', foreshadow: '伏笔', foreshadows: '伏笔', writing_styles: '写作风格' }
  const [verb, ...rest] = name.split('_')
  return verbs[verb] && nouns[rest.join('_')] ? `${verbs[verb]}${nouns[rest.join('_')]}` : '工具调用'
}

export type AgentTimelineItem = { key: string; type: 'message'; message: AgentMessage } | { key: string; type: 'tool'; step: AgentToolStep } | { key: string; type: 'change'; change: AgentChange }
/** tool_call_id 去重：恢复的历史和重连后的实时事件在同一位置更新。 */
export function buildAgentTimeline(messages: AgentMessage[], changes: AgentChange[], live: AgentToolStep[]): AgentTimelineItem[] {
  const result: AgentTimelineItem[] = []
  const calls = new Set<string>()
  const shownChanges = new Set<number>()
  const addTool = (callId: string, name: string, turnId: number | null | undefined, turnKey: string | null | undefined, origin: string) => {
    const bound = hasTaskIdentity({ id: turnId ?? 0, task_key: turnKey ?? null })
    const key = `${turnId ?? 'history'}:${bound ? turnKey : `unbound-${origin}`}:${callId}`
    if (calls.has(key)) return
    calls.add(key)
    const output = bound ? messages.find((m) => m.role === 'tool' && m.tool_call_id === callId && m.turn_task_id === turnId && m.turn_task_key === turnKey) : undefined
    const step = bound ? live.find((s) => s.call_id === callId && s.turn_task_id === turnId && s.turn_task_key === turnKey) : undefined
    result.push({ key: `tool-${key}`, type: 'tool', step: { call_id: callId, name, label: toolLabel(name), turn_task_id: turnId ?? null, turn_task_key: turnKey ?? null, ...(output ? { ok: !output.content.includes('"error"'), summary: output.content } : {}), ...step } })
    if (bound) changes.filter((c) => c.tool_call_id === callId && c.turn_task_id === turnId && c.turn_task_key === turnKey).forEach((change) => { shownChanges.add(change.id); result.push({ key: `change-${change.id}:${change.change_key ?? 'unknown'}`, type: 'change', change }) })
  }
  for (const message of messages) {
    if (message.role === 'tool') {
      if (message.tool_call_id) addTool(message.tool_call_id, message.tool_name ?? '', message.turn_task_id, message.turn_task_key, `message-${message.id}`)
      else if (message.content) result.push({ key: `message-${message.id}`, type: 'message', message })
      continue
    }
    if (message.content) result.push({ key: `message-${message.id}`, type: 'message', message })
    for (const call of message.tool_calls ?? []) addTool(call.id, call.name, message.turn_task_id, message.turn_task_key, `message-${message.id}`)
  }
  for (const [index, step] of live.entries()) addTool(step.call_id, step.name, step.turn_task_id, step.turn_task_key, `live-${index}`)
  for (const change of changes) if (!shownChanges.has(change.id)) result.push({ key: `change-${change.id}:${change.change_key ?? 'unknown'}`, type: 'change', change })
  return result
}

const TABLE_RESOURCES: Record<string, ProjectResource> = {
  projects: 'project', characters: 'characters', organizations: 'organizations', organization_members: 'organizations',
  relationships: 'relationships', relationship_history: 'relationships', careers: 'careers', career_stages: 'careers', character_careers: 'careers', career_history: 'careers',
  world_settings: 'world', outlines: 'outlines', chapter_plans: 'outlines', chapters: 'chapters', chapter_characters: 'chapters',
  foreshadows: 'foreshadows', foreshadow_characters: 'foreshadows', foreshadow_history: 'foreshadows', story_memories: 'memories',
}
export function changedResources(changes: AgentChange[]): ProjectResource[] {
  const resources = new Set<ProjectResource>(['project'])
  for (const change of changes) {
    for (const effect of change.effects ?? []) if (TABLE_RESOURCES[effect.table]) resources.add(TABLE_RESOURCES[effect.table])
    if (change.tool_name.includes('chapter')) { resources.add('chapters'); resources.add('memories') }
    if (change.tool_name.includes('character')) resources.add('characters')
    if (change.tool_name.includes('outline') || change.tool_name.includes('plans')) { resources.add('outlines'); resources.add('chapters') }
    if (change.tool_name.includes('organization') || change.tool_name.includes('org_member')) resources.add('organizations')
    if (change.tool_name.includes('relationship')) resources.add('relationships')
    if (change.tool_name.includes('career')) resources.add('careers')
    if (change.tool_name.includes('world')) resources.add('world')
    if (change.tool_name.includes('foreshadow')) resources.add('foreshadows')
  }
  return [...resources]
}

/** 协作授权失效时系统作废本轮待确认提案的固定文案（后端 COLLABORATION_CHANGE_VOIDED，裁决 25）。 */
export const AGENT_VOIDED_ERROR = '协作授权已失效，提案已作废'
/** 续跑时模型配置失效的失败文案（作者向量配置变化、本人或作者文本模型配置被改 / 删均会出现；裁决 27、28）。 */
export const AGENT_MODEL_INVALID_ERROR = '模型配置已失效，请重新选择'
export const AGENT_MODEL_CHANGED_HINT = '模型配置已变化（可能是本人或作者的配置），请在会话中重新发起'
/** 协作者通用重试被拒（403 / 400 / 409 不统一，裁决 24）的统一提示。 */
export const AGENT_RETRY_REJECTED_HINT = '不可重试，请在当前会话重新发起'

type OpenTurn = Pick<Task, 'id' | 'task_key' | 'status'>

/** 提案所属轮次仍未结束（排队 / 运行 / 待确认），且与该轮次实例一致。 */
export function isAgentTurnOpen(change: Pick<AgentChange, 'turn_task_id' | 'turn_task_key'>, turn: OpenTurn | null | undefined): boolean {
  return !!turn && isOpenTask(turn.status) && change.turn_task_id === turn.id && !!turn.task_key
    && change.turn_task_key === turn.task_key
}

/** 工具步骤没有结果、所属轮次也已结束（失败 / 取消 / 授权失效作废 / 中断）时的状态文案。 */
export const AGENT_TOOL_STOPPED = '已停止'
/**
 * 工具步骤状态：有结果按成败显示；无结果时只有所属轮次仍未结束（或正在实时订阅该步骤）才显示「处理中」，
 * 否则显示「已停止」，避免已结束轮次残留「处理中」（E4i-h）。
 */
export function agentToolStepState(step: Pick<AgentToolStep, 'ok' | 'turn_task_id' | 'turn_task_key'>, turn: OpenTurn | null | undefined, live = false) {
  if (step.ok !== undefined) return { label: step.ok ? '完成' : '未完成', stopped: false }
  const open = live || isAgentTurnOpen({ turn_task_id: step.turn_task_id ?? null, turn_task_key: step.turn_task_key ?? null }, turn)
  return { label: open ? '处理中' : AGENT_TOOL_STOPPED, stopped: !open }
}

/**
 * 提案卡片状态：协作授权失效作废的提案显示「已作废」（裁决 25）；所属轮次已结束仍残留的待确认提案
 * 显示「轮次已结束」并置灰（裁决 26，服务端不事后补作废，也已无法确认）。
 */
export function agentChangeState(change: Pick<AgentChange, 'status' | 'error' | 'turn_task_id' | 'turn_task_key'>, turn: OpenTurn | null | undefined, timeline = true) {
  if (change.status === 'failed' && change.error === AGENT_VOIDED_ERROR) return { label: '已作废', color: 'default', ended: false }
  if (timeline && change.status === 'proposed' && !isAgentTurnOpen(change, turn)) return { label: '轮次已结束', color: 'default', ended: true }
  return { ...AGENT_CHANGE_STATUS[change.status], ended: false }
}

/**
 * 「重试本轮」：协作者因作者向量配置变化失败的轮次重试必然再次失败，隐藏并提示重新发起（裁决 27）；
 * 协作者重试已被服务端拒绝（rejected）时同样隐藏并统一提示（裁决 24）。作者行为不变。
 */
export function agentRetryState(task: Pick<Task, 'status' | 'error' | 'kind'>, collaborator: boolean, rejected: boolean): { retry: boolean; hint: string | null } {
  if (collaborator && task.kind === 'agent.turn' && (task.error ?? '').includes(AGENT_MODEL_INVALID_ERROR)) {
    return { retry: false, hint: AGENT_MODEL_CHANGED_HINT }
  }
  if (collaborator && rejected) return { retry: false, hint: AGENT_RETRY_REJECTED_HINT }
  return { retry: canRetryTask(task.status), hint: null }
}

/** 会话只读原因（服务端 writable=false 时）：查看者、或撤权后重邀留下的旧轮次会话（裁决 4、7）。 */
export function agentReadOnlyNotice(writable: boolean, canUseAgent: boolean): string | null {
  if (writable) return null
  return canUseAgent
    ? '此会话属于之前的协作授权，只能查看；请新开会话继续。'
    : '当前为查看者，只能查看本人会话，不能发送、确认、拒绝或撤销。'
}
