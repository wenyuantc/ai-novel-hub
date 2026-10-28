import type { AgentChange, AgentChangeStatus, AgentExternalCall, AgentMessage, AgentToolEnd, AgentToolStart } from '../api/types'
import type { ProjectResource } from './projectRefresh'

export const AGENT_CHANGE_STATUS: Record<AgentChangeStatus, { label: string; color: string }> = {
  proposed: { label: '待确认', color: 'gold' }, executing: { label: '执行中', color: 'processing' },
  executed: { label: '已执行', color: 'success' }, rejected: { label: '已拒绝', color: 'default' },
  failed: { label: '执行失败', color: 'error' }, undone: { label: '已撤销', color: 'default' },
}
export interface AgentToolStep extends AgentToolStart { ok?: boolean; summary?: string; turn_task_id?: number | null }
export function mergeToolStep(steps: AgentToolStep[], event: (AgentToolStart | AgentToolEnd) & { turn_task_id?: number | null }): AgentToolStep[] {
  const old = steps.find((s) => s.call_id === event.call_id && s.turn_task_id === event.turn_task_id)
  const next = { name: '', label: '工具调用', ...old, ...event }
  return old ? steps.map((s) => s.call_id === event.call_id && s.turn_task_id === event.turn_task_id ? next : s) : [...steps, next]
}
export function upsertAgentChange(changes: AgentChange[], change: AgentChange): AgentChange[] {
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
export function changeTaskId(change: AgentChange): number | null {
  if (change.kind !== 'task' || change.status !== 'executed') return null
  if (typeof change.result?.task_id === 'number') return change.result.task_id
  const task = change.result?.task
  return typeof task === 'object' && task !== null && 'id' in task && typeof task.id === 'number' ? task.id : null
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
  const addTool = (callId: string, name: string, turnId: number | null | undefined) => {
    const key = `${turnId ?? 'history'}:${callId}`
    if (calls.has(key)) return
    calls.add(key)
    const output = messages.find((m) => m.role === 'tool' && m.tool_call_id === callId && m.turn_task_id === turnId)
    const step = live.find((s) => s.call_id === callId && s.turn_task_id === turnId)
    result.push({ key: `tool-${key}`, type: 'tool', step: { call_id: callId, name, label: toolLabel(name), ...(output ? { ok: !output.content.includes('"error"'), summary: output.content } : {}), ...step } })
    changes.filter((c) => c.tool_call_id === callId && c.turn_task_id === turnId).forEach((change) => { shownChanges.add(change.id); result.push({ key: `change-${change.id}`, type: 'change', change }) })
  }
  for (const message of messages) {
    if (message.role === 'tool') {
      if (message.tool_call_id) addTool(message.tool_call_id, message.tool_name ?? '', message.turn_task_id)
      else if (message.content) result.push({ key: `message-${message.id}`, type: 'message', message })
      continue
    }
    if (message.content) result.push({ key: `message-${message.id}`, type: 'message', message })
    for (const call of message.tool_calls ?? []) addTool(call.id, call.name, message.turn_task_id)
  }
  for (const step of live) addTool(step.call_id, step.name, step.turn_task_id)
  for (const change of changes) if (!shownChanges.has(change.id)) result.push({ key: `change-${change.id}`, type: 'change', change })
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
