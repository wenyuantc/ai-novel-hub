// Skill 自定义输入项的纯函数：默认值、必填 / 取值校验、请求载荷、正文占位警告（与后端 app/ai/skill_inputs.py 同口径）

import type { SkillBrief, SkillInputDef, SkillInputKind, SkillInputValues } from '../api/types'

const NAME_PATTERN = '[A-Za-z_\\u4e00-\\u9fff][A-Za-z0-9_\\u4e00-\\u9fff]{0,31}'
/** 输入项标识：字母 / 数字 / 下划线 / 汉字，不以数字开头，≤32 */
export const SKILL_INPUT_NAME_RE = new RegExp(`^${NAME_PATTERN}$`)
/** 正文中的占位 {{ 标识 }}（不含组件引用 {{> x }}） */
const PLACEHOLDER_RE = new RegExp(`\\{\\{\\s*(${NAME_PATTERN})\\s*\\}\\}`, 'g')

export const SKILL_INPUT_KIND_LABELS: Record<SkillInputKind, string> = { text: '单行文本', textarea: '多行文本', select: '单选下拉' }
/** 各类型取值上限（字数，按码点） */
export const SKILL_INPUT_VALUE_LIMITS: Record<SkillInputKind, number> = { text: 200, textarea: 5000, select: 50 }

const chars = (text: string) => Array.from(text).length

/** 正文中形如 {{ 标识 }} 的占位标识（去重保序） */
export function placeholders(text: string): string[] {
  if (!text || !text.includes('{{')) return []
  const result: string[] = []
  for (const m of text.matchAll(PLACEHOLDER_RE)) if (!result.includes(m[1])) result.push(m[1])
  return result
}

/**
 * 编辑时的占位提示（不阻止保存）：正文里形如占位却未声明、声明了却从未引用。
 * 前端按草稿正文原文计算（不展开组件）；保存后以后端 input_warnings（展开组件后）为准。
 */
export function inputWarnings(defs: Pick<SkillInputDef, 'name'>[], body: string): string[] {
  const declared = defs.map((d) => d.name.trim()).filter(Boolean)
  const found = placeholders(body)
  return [
    ...found.filter((n) => !declared.includes(n)).sort().map((n) => `正文中的 {{ ${n} }} 没有声明为输入项，将原样保留`),
    ...declared.filter((n) => !found.includes(n)).map((n) => `输入项「${n}」没有在正文中引用`),
  ]
}

/** 以默认值预填 */
export function defaultInputValues(defs: SkillInputDef[]): SkillInputValues {
  return Object.fromEntries(defs.map((d) => [d.name, d.default ?? '']))
}

/** 当前值：只取已声明的标识，未填写的用默认值 */
export function resolveInputValues(defs: SkillInputDef[], inputs: SkillInputValues): SkillInputValues {
  return Object.fromEntries(defs.map((d) => [d.name, Object.hasOwn(inputs, d.name) ? inputs[d.name] : d.default ?? '']))
}

/** 单项问题：必填为空 / 超长 / 下拉值不在选项中；没有问题返回 null */
export function inputProblem(def: SkillInputDef, value: string): string | null {
  if (!value.trim()) return def.required ? '必填' : null
  const limit = SKILL_INPUT_VALUE_LIMITS[def.kind] ?? SKILL_INPUT_VALUE_LIMITS.text
  if (chars(value) > limit) return `不能超过 ${limit} 字`
  if (def.kind === 'select' && !def.options.includes(value)) return '取值不在选项中'
  return null
}

/** 必填但为空的输入项 */
export function missingRequired(defs: SkillInputDef[], values: SkillInputValues): SkillInputDef[] {
  return defs.filter((d) => d.required && !(values[d.name] ?? '').trim())
}

/** 请求里的 Skill 字段：只在选了 Skill 时带 skill_id；只有声明了输入项时才带 skill_inputs */
export interface SkillPayload { skill_id?: number; skill_inputs?: SkillInputValues }

export interface SkillChoiceState {
  skill: SkillBrief | null
  values: SkillInputValues
  /** 有问题的输入项（标识 → 说明） */
  problems: Record<string, string>
  valid: boolean
  payload: SkillPayload
}

/**
 * 由选中的 Skill 与已填写的值推导表单状态。Skill 列表尚未加载 / 已不存在时只带 skill_id（服务端校验）。
 */
export function skillChoiceState(skills: SkillBrief[], skillId: number | null, inputs: SkillInputValues): SkillChoiceState {
  if (skillId === null) return { skill: null, values: {}, problems: {}, valid: true, payload: {} }
  const skill = skills.find((s) => s.id === skillId) ?? null
  const defs = skill?.inputs ?? []
  const values = resolveInputValues(defs, inputs)
  const problems: Record<string, string> = {}
  for (const d of defs) {
    const problem = inputProblem(d, values[d.name])
    if (problem) problems[d.name] = problem
  }
  const payload: SkillPayload = defs.length ? { skill_id: skillId, skill_inputs: values } : { skill_id: skillId }
  return { skill, values, problems, valid: Object.keys(problems).length === 0, payload }
}

/** 无法提交时的提示文案 */
export function skillChoiceProblemText(state: Pick<SkillChoiceState, 'skill' | 'problems'>): string | null {
  const names = Object.keys(state.problems)
  if (!names.length || !state.skill) return null
  const labels = state.skill.inputs.filter((d) => names.includes(d.name)).map((d) => d.label)
  return `请先填写 Skill「${state.skill.name}」的输入项：${labels.join('、')}`
}

/** 输入项编辑器的单项校验（与后端 SkillInputDef 一致）；返回错误说明列表 */
export function inputDefErrors(defs: Partial<SkillInputDef>[]): string[] {
  const errors: string[] = []
  if (defs.length > 20) errors.push('输入项最多 20 个')
  const names = defs.map((d) => (d.name ?? '').trim())
  const dup = [...new Set(names.filter((n, i) => n && names.indexOf(n) !== i))]
  if (dup.length) errors.push(`输入项标识重复：${dup.join('、')}`)
  defs.forEach((d, i) => {
    const label = (d.label ?? '').trim() || `第 ${i + 1} 项`
    const kind = d.kind ?? 'text'
    const options = (d.options ?? []).map((o) => o.trim())
    const def = d.default ?? ''
    if (kind === 'select') {
      if (options.length < 2 || options.length > 50) errors.push(`下拉输入项「${label}」须有 2–50 个选项`)
      if (def && !options.includes(def)) errors.push(`下拉输入项「${label}」的默认值须是选项之一`)
    }
    if (chars(def) > SKILL_INPUT_VALUE_LIMITS[kind]) errors.push(`输入项「${label}」的默认值不超过 ${SKILL_INPUT_VALUE_LIMITS[kind]} 字`)
  })
  return errors
}

/** 编辑器草稿 → 保存载荷（去空白、非下拉清空选项） */
export function normalizeInputDefs(defs: Partial<SkillInputDef>[]): SkillInputDef[] {
  return defs.map((d) => {
    const kind = d.kind ?? 'text'
    return {
      name: (d.name ?? '').trim(),
      label: (d.label ?? '').trim(),
      help: (d.help ?? '').trim(),
      kind,
      required: !!d.required,
      default: d.default ?? '',
      options: kind === 'select' ? [...new Set((d.options ?? []).map((o) => o.trim()).filter(Boolean))] : [],
    }
  })
}

/** 下层组件（局部改写、区块改写）只需要的部分：请求载荷与能否提交 */
export type SkillRequestChoice = Pick<SkillChoiceState, 'skill' | 'problems' | 'valid' | 'payload'>
/** 未选 Skill */
export const NO_SKILL_CHOICE: SkillRequestChoice = { skill: null, problems: {}, valid: true, payload: {} }
