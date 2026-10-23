// 角色：定位 / 存活状态标签、组合保存（with-relations）、所属组织、AI 生成与旧关系解析任务

import { api } from './client'
import type {
  CharacterGenerateRequest,
  CharacterWithRelations,
  CharacterWithRelationsOut,
  LifeStatus,
  Member,
  RelationsParseRequest,
  Task,
} from './types'

/** 定位下拉选项；旧数据的自由文本值仍照常显示并可保留（见 roleTypeOptions） */
export const ROLE_TYPES = ['主角', '重要配角', '配角', '反派', '其他'] as const

export const LIFE_STATUS: Record<LifeStatus, { label: string; color: string }> = {
  alive: { label: '存活', color: 'green' },
  dead: { label: '死亡', color: 'default' },
  missing: { label: '失踪', color: 'orange' },
  other: { label: '其他', color: 'blue' },
}

export function lifeStatusLabel(status: string): { label: string; color: string } {
  return status in LIFE_STATUS ? LIFE_STATUS[status as LifeStatus] : { label: status || '-', color: 'default' }
}

/** 与后端 cast_service.is_protagonist 一致 */
export function isProtagonist(c: { always_in_context?: boolean; role_type: string }): boolean {
  if (c.always_in_context) return true
  const r = c.role_type.trim().toLowerCase()
  return r === '主角' || r === 'protagonist'
}

/** 定位下拉选项：预设值 + 当前不在预设里的旧自由文本值（排在最后，原样保留） */
export function roleTypeOptions(current?: string | null): { value: string; label: string }[] {
  const opts: { value: string; label: string }[] = ROLE_TYPES.map((r) => ({ value: r, label: r }))
  const cur = current?.trim()
  if (cur && !(ROLE_TYPES as readonly string[]).includes(cur)) opts.push({ value: cur, label: `${cur}（原值）` })
  return opts
}

/** 角色卡片定位标签颜色 */
export function roleTypeColor(roleType: string): string {
  switch (roleType.trim()) {
    case '主角':
      return 'gold'
    case '重要配角':
      return 'blue'
    case '配角':
      return 'cyan'
    case '反派':
      return 'red'
    default:
      return 'purple'
  }
}

/** 别名清洗：与后端 clean_aliases 一致（trim、去空、去重保序），返回错误文案或清洗结果 */
export function cleanAliases(values: string[]): { aliases: string[]; error: string | null } {
  const out: string[] = []
  for (const raw of values) {
    const v = raw.trim()
    if (!v || out.includes(v)) continue
    if (v.length > 50) return { aliases: out, error: '单个别名不能超过 50 字' }
    out.push(v)
  }
  if (out.length > 10) return { aliases: out, error: '别名最多 10 个' }
  return { aliases: out, error: null }
}

export const characterExtraApi = {
  /** 创建角色并同时创建关系（单事务） */
  withRelations: (pid: number, body: CharacterWithRelations) =>
    api.post<CharacterWithRelationsOut>(`/projects/${pid}/characters/with-relations`, body),
  memberships: (pid: number, cid: number) => api.get<Member[]>(`/projects/${pid}/characters/${cid}/memberships`),
  /** AI 生成单个角色：202 任务（character.generate），只产出草稿 */
  generate: (pid: number, body: CharacterGenerateRequest) => api.post<Task>(`/projects/${pid}/characters/generate`, body),
  /** AI 解析旧关系备注：202 任务（relations.parse），只产出建议 */
  parseRelations: (pid: number, cid: number, body: RelationsParseRequest) =>
    api.post<Task>(`/projects/${pid}/characters/${cid}/parse-relations`, body),
}
