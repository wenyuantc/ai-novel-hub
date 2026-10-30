// 关系类型、关系、历史与图谱

import { api } from './client'
import type {
  Relationship,
  RelationshipCreate,
  RelationshipGraph,
  RelationshipHistory,
  RelationshipType,
  RelationshipTypeInput,
  RelationshipUpdate,
  RelationCategory,
  RelationStatus,
  RelationsGenerateRequest,
  Task,
} from './types'

const p = (pid: number) => `/projects/${pid}`

export const relationshipTypeApi = {
  /** 预置在前，再是本项目自定义 */
  list: (pid: number) => api.get<RelationshipType[]>(`${p(pid)}/relationship-types`),
  create: (pid: number, body: RelationshipTypeInput) => api.post<RelationshipType>(`${p(pid)}/relationship-types`, body),
  remove: (pid: number, tid: number) => api.del(`${p(pid)}/relationship-types/${tid}`),
}

export const relationshipApi = {
  /** 生成关系草稿（202 后台任务），不直接写入关系 */
  generate: (pid: number, body: RelationsGenerateRequest) => api.post<Task>(`${p(pid)}/relationships/generate`, body),
  /** 传 characterId 时返回该角色视角字段（other_* / self_role） */
  list: (pid: number, characterId?: number) =>
    api.get<Relationship[]>(`${p(pid)}/relationships`, { character_id: characterId }),
  create: (pid: number, body: RelationshipCreate) => api.post<Relationship>(`${p(pid)}/relationships`, body),
  /** 批量创建（单事务，任一失败全部回滚） */
  batch: (pid: number, items: RelationshipCreate[]) =>
    api.post<Relationship[]>(`${p(pid)}/relationships/batch`, { items }),
  update: (pid: number, rid: number, body: RelationshipUpdate) =>
    api.patch<Relationship>(`${p(pid)}/relationships/${rid}`, body),
  remove: (pid: number, rid: number) => api.del(`${p(pid)}/relationships/${rid}`),
  history: (pid: number, rid: number) => api.get<RelationshipHistory[]>(`${p(pid)}/relationships/${rid}/history`),
  graph: (pid: number) => api.get<RelationshipGraph>(`${p(pid)}/relationship-graph`),
}

export const RELATION_CATEGORY: Record<RelationCategory, { label: string; color: string }> = {
  family: { label: '家庭', color: 'gold' },
  social: { label: '社交', color: 'blue' },
  hostile: { label: '敌对', color: 'red' },
  work: { label: '职场', color: 'cyan' },
  other: { label: '其他', color: 'default' },
}

export const RELATION_STATUS: Record<RelationStatus, { label: string; color: string }> = {
  normal: { label: '正常', color: 'green' },
  broken: { label: '破裂', color: 'red' },
  past: { label: '过去式', color: 'default' },
  complicated: { label: '复杂', color: 'purple' },
}

export const RELATION_SOURCE_LABELS: Record<string, string> = {
  manual: '手动',
  wizard: '向导',
  ai_parse: 'AI 解析',
  ai_generate: 'AI 生成',
  import: '导入',
  analysis: '章节分析',
}

export const HISTORY_FIELD_LABELS: Record<string, string> = {
  type: '类型',
  direction: '方向',
  intimacy: '亲密度',
  status: '状态',
  description: '描述',
  a_id: '一方',
  b_id: '另一方',
  type_id: '类型',
}

export function categoryLabel(c: string): { label: string; color: string } {
  return c in RELATION_CATEGORY ? RELATION_CATEGORY[c as RelationCategory] : { label: c || '-', color: 'default' }
}

export function statusLabel(s: string): { label: string; color: string } {
  return s in RELATION_STATUS ? RELATION_STATUS[s as RelationStatus] : { label: s || '-', color: 'default' }
}

export function isSymmetric(t: { role_a: string; role_b: string }): boolean {
  return t.role_a === t.role_b
}

/** 关系的一句话描述（全局视角）：「A 是 B 的师父」/「A 与 B 是朋友」 */
export function describeRelation(r: Pick<Relationship, 'a_name' | 'b_name' | 'role_a' | 'role_b'>): string {
  return isSymmetric(r) ? `${r.a_name} 与 ${r.b_name} 是${r.role_a}` : `${r.a_name} 是 ${r.b_name} 的${r.role_a}`
}

/** viewer 视角：对方在我眼中的身份与我自己的身份；viewer 不是这段关系的一方时为 null */
export function perspective(
  r: Pick<Relationship, 'a_id' | 'b_id' | 'a_name' | 'b_name' | 'role_a' | 'role_b'>,
  viewerId: number,
): { otherId: number; otherName: string; otherRole: string; selfRole: string } | null {
  if (r.a_id === viewerId) return { otherId: r.b_id, otherName: r.b_name, otherRole: r.role_b, selfRole: r.role_a }
  if (r.b_id === viewerId) return { otherId: r.a_id, otherName: r.a_name, otherRole: r.role_a, selfRole: r.role_b }
  return null
}

/** 关系类型下拉选项：value = `${type_id}:a|b`（viewer 是 a / b 一方）；对称类型只有一个选项 */
export interface TypeOption {
  value: string
  label: string
  typeId: number
  viewerIsA: boolean
  category: string
}

export function typeOptionValue(typeId: number, viewerIsA: boolean): string {
  return `${typeId}:${viewerIsA ? 'a' : 'b'}`
}

export function parseTypeOptionValue(value: string): { typeId: number; viewerIsA: boolean } | null {
  const m = /^(\d+):(a|b)$/.exec(value)
  return m ? { typeId: Number(m[1]), viewerIsA: m[2] === 'a' } : null
}

/**
 * 按 viewer 视角生成类型选项：「对方是{self}的徒弟」（viewer 是 role_a=师父 一方）/「对方是{self}的师父」。
 * selfName 默认「我」。
 */
export function typeOptions(types: RelationshipType[], selfName = '我', otherName = '对方'): TypeOption[] {
  const out: TypeOption[] = []
  for (const t of types) {
    if (isSymmetric(t)) {
      out.push({
        value: typeOptionValue(t.id, true),
        label: `${otherName}与${selfName}是${t.role_a}`,
        typeId: t.id,
        viewerIsA: true,
        category: t.category,
      })
      continue
    }
    out.push({
      value: typeOptionValue(t.id, true),
      label: `${otherName}是${selfName}的${t.role_b}（${selfName}是${t.role_a}）`,
      typeId: t.id,
      viewerIsA: true,
      category: t.category,
    })
    out.push({
      value: typeOptionValue(t.id, false),
      label: `${otherName}是${selfName}的${t.role_a}（${selfName}是${t.role_b}）`,
      typeId: t.id,
      viewerIsA: false,
      category: t.category,
    })
  }
  return out
}

/** 亲密度 → 颜色：-100 红 → 0 灰 → 100 绿（线性插值，返回 #rrggbb） */
export function intimacyColor(intimacy: number): string {
  const v = Math.max(-100, Math.min(100, Number.isFinite(intimacy) ? intimacy : 0)) / 100
  const gray = [140, 140, 140]
  const red = [245, 34, 45]
  const green = [82, 196, 26]
  const target = v < 0 ? red : green
  const t = Math.abs(v)
  const mix = gray.map((g, i) => Math.round(g + (target[i] - g) * t))
  return `#${mix.map((c) => c.toString(16).padStart(2, '0')).join('')}`
}

/** 历史 changes 中的值转展示文本 */
export function formatChangeValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '（空）'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}
