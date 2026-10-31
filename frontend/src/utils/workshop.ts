import type { PromptTemplateDetail, WorkshopCategory, WorkshopItem, WorkshopSubmission, WorkshopSubmissionStatus, WorkshopSubmit, WritingStyle } from '../api/types'
import { STYLE_FIELD_KEYS } from './styles'

export const WORKSHOP_CATEGORIES: Record<WorkshopCategory, string> = {
  general: '通用', xuanhuan: '玄幻仙侠', wuxia: '武侠', romance: '言情', scifi: '科幻',
  suspense: '悬疑', history: '历史', urban: '都市', other: '其他',
}
export const WORKSHOP_KINDS = { style: '写作风格', prompt: '提示词模板' }
export const SUBMISSION_STATUS: Record<WorkshopSubmissionStatus, { label: string; color: string }> = {
  pending: { label: '待审核', color: 'gold' }, approved: { label: '已通过', color: 'green' },
  rejected: { label: '已驳回', color: 'red' }, cancelled: { label: '已撤回', color: 'default' },
}
export const workshopCategoryOptions = Object.entries(WORKSHOP_CATEGORIES).map(([value, label]) => ({ value, label }))

/** 查询串只决定已授权的页签，不能授予普通用户审核入口。 */
export function workshopTab(value: string | null, isAdmin: boolean): 'browse' | 'mine' | 'review' {
  if (value === 'mine') return 'mine'
  if (value === 'review' && isAdmin) return 'review'
  return 'browse'
}

export function normalizeWorkshopTags(tags: string[]): string[] {
  return [...new Set(tags.map((t) => t.trim()).filter(Boolean))]
}
export function workshopTagsError(tags: string[]): string | null {
  if (tags.length > 5) return '最多填写 5 个标签'
  if (tags.some((t) => !t.trim() || Array.from(t).length > 12 || /["\\\n\r]/.test(t))) return '标签须为 1–12 字，不能包含引号、反斜线或换行'
  return null
}
export function styleShare(style: WritingStyle): WorkshopSubmit {
  return {
    kind: 'style', name: style.name, description: style.description, category: 'general', tags: [], is_anonymous: false,
    content: Object.fromEntries(STYLE_FIELD_KEYS.map((key) => [key, style[key]])),
  }
}
export function promptShare(detail: PromptTemplateDetail): WorkshopSubmit | null {
  if (!detail.override) return null
  return {
    kind: 'prompt', prompt_key: detail.key, name: detail.name, description: detail.description,
    category: 'general', tags: [], is_anonymous: false,
    content: { system: detail.override.system, user: detail.override.user },
  }
}
export function updateShare(item: WorkshopItem | WorkshopSubmission): WorkshopSubmit {
  return {
    kind: item.kind, item_id: 'is_mine' in item ? item.id : item.item_id, prompt_key: item.prompt_key,
    name: item.name, description: item.description, category: item.category, tags: item.tags,
    is_anonymous: item.is_anonymous, content: { ...item.content },
  }
}
export function canRestoreWorkshopItem(item: WorkshopItem, isAdmin: boolean): boolean {
  return item.status === 'hidden' && (isAdmin || (item.is_mine && item.hidden_by === 'author'))
}
/** 首次审核通过也会回写 item_id，通过后不能再靠该字段区分首次或更新。 */
export function workshopSubmissionKindLabel(submission: Pick<WorkshopSubmission, 'item_id' | 'status'>): string {
  return submission.status === 'approved' ? '已发布' : submission.item_id ? '条目更新' : '首次提交'
}
/** 停用的覆盖不生效：导入对比应使用当前系统默认。 */
export function workshopImportPairs(detail: PromptTemplateDetail, content: Record<string, string>) {
  const current = detail.override?.enabled ? detail.override : { system: detail.default_system, user: detail.default_user }
  return [
    { label: '系统段', a: current.system, b: content.system ?? '' },
    { label: '用户段', a: current.user, b: content.user ?? '' },
  ]
}
export function emptyWorkshopShare(kind: 'style' | 'prompt'): WorkshopSubmit {
  return {
    kind, name: '', description: '', category: 'general', tags: [], is_anonymous: false,
    ...(kind === 'prompt' ? { prompt_key: '' } : {}),
    content: kind === 'prompt' ? { system: '', user: '' } : Object.fromEntries(STYLE_FIELD_KEYS.map((k) => [k, ''])),
  }
}
