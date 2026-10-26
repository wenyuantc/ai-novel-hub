// 写作风格的纯函数：解析优先级的展示文案、下拉选项、分组、引用数、导入结果汇总

import { ApiError } from '../api/client'
import type {
  StyleBrief,
  StyleChoice,
  StyleCreate,
  StyleDraft,
  StyleFields,
  StyleImportResult,
  StyleReferences,
  WritingStyle,
} from '../api/types'

export type DimensionKey = 'pov' | 'rhythm' | 'dialogue' | 'description_density' | 'tone' | 'diction'

/** 维度字段：下拉给常用选项，也允许自由填写 */
export const STYLE_DIMENSIONS: { key: DimensionKey; label: string; options: string[] }[] = [
  {
    key: 'pov',
    label: '叙事视角',
    options: ['第一人称', '第三人称有限视角，紧跟主角', '第三人称全知视角', '多视角轮换'],
  },
  { key: 'rhythm', label: '句式节奏', options: ['短句为主，节奏明快', '长短句交错', '长句铺陈，节奏舒缓'] },
  { key: 'dialogue', label: '对白比例', options: ['对白偏少', '对白适中', '对白为主，推动情节'] },
  {
    key: 'description_density',
    label: '描写密度',
    options: ['低，只写对情节有用的细节', '中等', '高，调动多种感官'],
  },
  { key: 'tone', label: '基调', options: ['冷静克制', '轻松幽默', '温柔怅惘', '热血昂扬', '阴郁压抑', '悬疑紧张'] },
  {
    key: 'diction',
    label: '用词偏好',
    options: ['口语化、直白', '典雅、书面', '古风、半文半白', '动词准确，少用形容词'],
  },
]

export const STYLE_FIELD_KEYS: (keyof StyleFields)[] = [
  'description',
  'pov',
  'rhythm',
  'dialogue',
  'description_density',
  'tone',
  'diction',
  'taboos',
  'sample',
  'extra',
]

/** 已有风格 → 编辑草稿 */
export function styleToDraft(style: Pick<WritingStyle, 'name' | keyof StyleFields>): StyleDraft {
  const draft = { name: style.name } as StyleDraft
  for (const k of STYLE_FIELD_KEYS) draft[k] = style[k] ?? ''
  return draft
}

/** 草稿 → 创建 / 更新请求体（全部 trim） */
export function draftToStyleBody(draft: StyleDraft): StyleCreate {
  const body: StyleCreate = { name: draft.name.trim() }
  for (const k of STYLE_FIELD_KEYS) body[k] = (draft[k] ?? '').trim()
  return body
}

/** 预设与我的风格分开展示（保持后端顺序） */
export function groupStyles(styles: WritingStyle[]): { presets: WritingStyle[]; mine: WritingStyle[] } {
  return { presets: styles.filter((s) => s.is_preset), mine: styles.filter((s) => !s.is_preset) }
}

export interface StyleContext {
  /** 章节覆盖（null = 继承项目默认） */
  chapterStyleId?: number | null
  /** 项目默认 */
  projectDefault?: StyleBrief | null
  /** 可见风格列表（用于把 id 换成名称） */
  styles?: Pick<WritingStyle, 'id' | 'name'>[]
}

export function styleName(id: number, styles: Pick<WritingStyle, 'id' | 'name'>[] = []): string {
  return styles.find((s) => s.id === id)?.name ?? `风格 #${id}`
}

export type StyleSource = 'request' | 'chapter' | 'project' | 'none'

/** 按「临时 > 章节 > 项目默认 > 无」解析（与后端 style_service.resolve 一致），返回 id、名称与来源 */
export function resolveStyleChoice(
  choice: StyleChoice,
  ctx: StyleContext,
): { id: number | null; name: string | null; source: StyleSource } {
  if (choice === 'none') return { id: null, name: null, source: 'none' }
  if (typeof choice === 'number') return { id: choice, name: styleName(choice, ctx.styles), source: 'request' }
  if (ctx.chapterStyleId != null) {
    return { id: ctx.chapterStyleId, name: styleName(ctx.chapterStyleId, ctx.styles), source: 'chapter' }
  }
  if (ctx.projectDefault) return { id: ctx.projectDefault.id, name: ctx.projectDefault.name, source: 'project' }
  return { id: null, name: null, source: 'none' }
}

/** 「继承」实际会用哪个风格：本章风格「X」/ 项目默认「X」/ 不用风格 */
export function describeInherit(ctx: StyleContext): string {
  const r = resolveStyleChoice('inherit', ctx)
  if (r.source === 'chapter') return `本章风格「${r.name}」`
  if (r.source === 'project') return `项目默认「${r.name}」`
  return '不用风格'
}

/** 选择项的展示文案 */
export function styleChoiceLabel(choice: StyleChoice, ctx: StyleContext): string {
  if (choice === 'inherit') return `继承（${describeInherit(ctx)}）`
  if (choice === 'none') return '不用风格'
  return styleName(choice, ctx.styles)
}

export interface StyleOption {
  value: StyleChoice
  label: string
}

export interface StyleOptionGroup {
  label: string
  options: StyleOption[]
}

/**
 * 生成 / 润色 / 重写 / 改写 / 批量的风格下拉：继承 + 不用风格 + 按预设 / 我的分组的具体风格。
 * inheritLabel 给出时替换继承项文案（批量：按章节或项目默认）。
 */
export function styleChoiceOptions(
  styles: WritingStyle[],
  ctx: StyleContext,
  inheritLabel?: string,
): (StyleOption | StyleOptionGroup)[] {
  const { presets, mine } = groupStyles(styles)
  const toOption = (s: WritingStyle): StyleOption => ({ value: s.id, label: s.name })
  const out: (StyleOption | StyleOptionGroup)[] = [
    { value: 'inherit', label: inheritLabel ?? `继承（${describeInherit({ ...ctx, styles })}）` },
    { value: 'none', label: '不用风格' },
  ]
  if (mine.length) out.push({ label: '我的风格', options: mine.map(toOption) })
  if (presets.length) out.push({ label: '预设风格', options: presets.map(toOption) })
  return out
}

/** 设定项（项目默认 / 章节覆盖）的下拉：只有具体风格，空 = 不设 */
export function styleAssignOptions(styles: WritingStyle[]): StyleOptionGroup[] {
  const { presets, mine } = groupStyles(styles)
  const toOption = (s: WritingStyle) => ({ value: s.id, label: s.name })
  return [
    ...(mine.length ? [{ label: '我的风格', options: mine.map(toOption) }] : []),
    ...(presets.length ? [{ label: '预设风格', options: presets.map(toOption) }] : []),
  ]
}

/** 引用数文案：2 个项目、3 个章节；没有引用为空串 */
export function referencesText(refs: StyleReferences | null | undefined): string {
  if (!refs) return ''
  const parts: string[] = []
  if (refs.projects > 0) parts.push(`${refs.projects} 个项目`)
  if (refs.chapters > 0) parts.push(`${refs.chapters} 个章节`)
  return parts.join('、')
}

/** 删除 409 响应体里的引用数（不是该错误时返回 null） */
export function readStyleReferences(err: unknown): StyleReferences | null {
  if (!(err instanceof ApiError) || err.status !== 409 || !err.body) return null
  const refs = err.body.references
  if (typeof refs !== 'object' || refs === null) return null
  const r = refs as Record<string, unknown>
  return {
    projects: typeof r.projects === 'number' ? r.projects : 0,
    chapters: typeof r.chapters === 'number' ? r.chapters : 0,
  }
}

/** 导入结果汇总：已导入 N 个风格（M 个因重名已改名）；另有 K 条提示 */
export function summarizeStyleImport(result: StyleImportResult): string {
  const renamed = result.items.filter((i) => i.renamed_from).length
  let text = `已导入 ${result.items.length} 个风格`
  if (renamed) text += `（${renamed} 个因重名已改名）`
  if (result.warnings.length) text += `；${result.warnings.length} 条提示`
  return text
}

/** 示例段落字数提示（按码点，与后端 len() 一致） */
export function sampleLengthHint(text: string, limit = 2000): { text: string; over: boolean } {
  const n = Array.from(text).length
  return { text: `${n} / ${limit} 字`, over: n > limit }
}
