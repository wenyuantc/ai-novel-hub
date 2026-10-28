// Skill 的纯函数：分组、选择器选项、`/触发词` 补全与匹配（与后端 skill_service.match_trigger 一致）、生成记录收窄

import type { SkillBrief, SkillRef } from '../api/types'

export function splitSkills<T extends Pick<SkillBrief, 'scope'>>(skills: T[]): { mine: T[]; global: T[] } {
  return { mine: skills.filter((s) => s.scope === 'mine'), global: skills.filter((s) => s.scope === 'global') }
}

/** 我的在前（同名触发词时优先），其后为全局 */
function ordered(skills: SkillBrief[]): SkillBrief[] {
  const { mine, global } = splitSkills(skills)
  return [...mine, ...global]
}

/** 下拉选项：分「我的」「全局」两组 */
export function skillSelectOptions(skills: SkillBrief[]) {
  const { mine, global } = splitSkills(skills)
  // 副本与全局 Skill 常同名，选项附带标识以便区分
  const option = (s: SkillBrief) => ({ value: s.id, label: `${s.name}（${s.slug}）`, title: s.description || s.name })
  return [
    ...(mine.length ? [{ label: '我的', options: mine.map(option) }] : []),
    ...(global.length ? [{ label: '全局', options: global.map(option) }] : []),
  ]
}

/** 输入框里正在输入的 `/前缀`（还没输入空白时）；不是补全状态返回 null */
export function slashQuery(input: string): string | null {
  const m = /^\/(\S*)$/u.exec(input)
  return m ? m[1].toLocaleLowerCase() : null
}

export interface TriggerOption {
  /** 选中后写入输入框的文本 */
  value: string
  trigger: string
  skill: SkillBrief
}

/** `/` 补全候选：触发词前缀匹配（大小写不敏感）；同一触发词只保留优先级最高的 Skill（我的优先） */
export function triggerOptions(input: string, skills: SkillBrief[], limit = 10): TriggerOption[] {
  const query = slashQuery(input)
  if (query === null) return []
  const seen = new Set<string>()
  const result: TriggerOption[] = []
  for (const skill of ordered(skills)) {
    for (const trigger of skill.triggers) {
      const key = trigger.toLocaleLowerCase()
      if (seen.has(key) || !key.startsWith(query)) continue
      seen.add(key)
      result.push({ value: `/${trigger} `, trigger, skill })
    }
  }
  return result.slice(0, limit)
}

/** 消息开头的 `/词`（其后为空白或结束）命中的 Skill；不做子串匹配 */
export function matchTrigger(input: string, skills: SkillBrief[]): SkillBrief | null {
  const m = /^\/(\S+)(?:\s|$)/u.exec(input)
  if (!m) return null
  const word = m[1].toLocaleLowerCase()
  return ordered(skills).find((s) => s.triggers.some((t) => t.toLocaleLowerCase() === word)) ?? null
}

/** 触发词编辑：去掉开头的 /、空白与重复（大小写不敏感） */
export function normalizeTriggers(values: string[]): string[] {
  const result: string[] = []
  for (const raw of values) {
    const value = raw.trim().replace(/^\/+/, '').trim()
    if (!value || /\s/u.test(value)) continue
    if (!result.some((t) => t.toLocaleLowerCase() === value.toLocaleLowerCase())) result.push(value)
  }
  return result
}

/** 由名称给出标识建议（只保留 ASCII 字母数字，其余转为连字符）；得不到时返回空串 */
export function suggestSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48)
    .replace(/-+$/g, '')
}

/** 生成记录 params.skill / 任务 skill 的收窄：{id, name} 或 null */
export function skillRef(value: unknown): SkillRef | null {
  if (typeof value !== 'object' || value === null) return null
  const { id, name } = value as Record<string, unknown>
  return typeof id === 'number' && typeof name === 'string' ? { id, name } : null
}
