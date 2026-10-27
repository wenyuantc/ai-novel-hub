// 项目备份与导入的纯函数：统计表格标签、大小格式化与估算、可选项、上传前检查、错误解读

import { ApiError } from '../api/client'
import type { BackupEstimate, BackupOptionKey, BackupOptions, ImportStyleAction } from '../api/types'

/** 与后端 importer.MAX_BYTES 一致 */
export const BACKUP_MAX_BYTES = 100 * 1024 * 1024
/** 估算超过该值时提示「备份较大」 */
export const BACKUP_LARGE_BYTES = 30 * 1024 * 1024
export const BACKUP_TOO_LARGE = '备份文件过大，上限为 100MB'
/** 与后端 importer 令牌过期文案一致 */
export const TOKEN_EXPIRED_TEXT = '导入凭据已过期'

/** 表名 → 中文标签（与后端 importer.TABLE_LABELS 一致；顺序即统计表格的展示顺序） */
export const TRANSFER_TABLE_LABELS: Record<string, string> = {
  world_settings: '世界观',
  characters: '角色',
  organizations: '组织',
  organization_members: '组织成员',
  relationship_types: '关系类型',
  relationships: '关系',
  relationship_history: '关系历史',
  careers: '职业',
  career_stages: '职业阶段',
  character_careers: '角色职业',
  career_history: '职业历史',
  outlines: '大纲',
  chapters: '章节',
  chapter_plans: '细纲',
  chapter_characters: '出场角色',
  foreshadows: '伏笔',
  foreshadow_characters: '伏笔角色',
  foreshadow_history: '伏笔历史',
  generation_logs: '生成记录',
  chapter_versions: '章节版本',
  chapter_analyses: '章节分析',
  analysis_changes: '分析回写条目',
  story_memories: '剧情记忆',
  kb_settings: '知识库设置',
  kb_documents: '知识库文档',
  outline_snapshots: '大纲快照',
  inspiration_sessions: '灵感会话',
}

export function tableLabel(table: string): string {
  return TRANSFER_TABLE_LABELS[table] ?? table
}

export interface CountRow {
  table: string
  label: string
  count: number
}

/** 统计表格行：已知表按固定顺序在前，未知表（新版本备份）按表名排在后面；0 行的不展示。 */
export function countRows(counts: Record<string, number>): CountRow[] {
  const known = Object.keys(TRANSFER_TABLE_LABELS)
  const unknown = Object.keys(counts)
    .filter((t) => !(t in TRANSFER_TABLE_LABELS))
    .sort()
  return [...known, ...unknown]
    .filter((t) => (counts[t] ?? 0) > 0)
    .map((t) => ({ table: t, label: tableLabel(t), count: counts[t] }))
}

/** 人类可读的大小：B / KB / MB / GB，KB 以上保留 1 位小数。 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  if (bytes < 1024) return `${Math.round(bytes)} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i += 1
  }
  return `${value.toFixed(1)} ${units[i]}`
}

export interface BackupOptionItem {
  key: BackupOptionKey
  label: string
  hint: string
}

export const BACKUP_OPTION_ITEMS: BackupOptionItem[] = [
  { key: 'versions', label: '章节版本', hint: '每章的历史版本，可在导入后继续回滚' },
  { key: 'snapshots', label: '大纲快照', hint: '大纲覆盖 / 重新展开前自动保存的快照' },
  { key: 'generation_logs', label: '生成记录', hint: '每次 AI 生成的提示词与参数，体积通常最大' },
  { key: 'inspiration', label: '关联的灵感会话', hint: '从本项目发起或转为本项目的灵感会话' },
]

export const DEFAULT_BACKUP_OPTIONS: BackupOptions = {
  versions: true,
  snapshots: false,
  generation_logs: false,
  inspiration: false,
}

/** 按勾选项估算备份总大小（核心数据始终包含）。 */
export function estimateTotal(estimate: BackupEstimate, options: BackupOptions): number {
  let total = estimate.core.bytes
  for (const { key } of BACKUP_OPTION_ITEMS) if (options[key]) total += estimate[key].bytes
  return total
}

/** 估算大小的提示：超过导入上限为 error，较大为 warning，否则 null。 */
export function backupSizeHint(bytes: number): { type: 'warning' | 'error'; text: string } | null {
  if (bytes > BACKUP_MAX_BYTES) {
    return {
      type: 'error',
      text: `估算约 ${formatBytes(bytes)}，超过导入上限 100MB，导出的文件将无法再导入；建议取消「生成记录」等可选项`,
    }
  }
  if (bytes > BACKUP_LARGE_BYTES) {
    return { type: 'warning', text: `估算约 ${formatBytes(bytes)}，备份较大，下载可能需要一些时间` }
  }
  return null
}

/** 包含的可选项文案（预览用）：「章节版本、大纲快照」，都没有时「无」。 */
export function includedOptionsText(options: BackupOptions): string {
  const names = BACKUP_OPTION_ITEMS.filter((i) => options[i.key]).map((i) => i.label)
  return names.length > 0 ? names.join('、') : '无'
}

export const STYLE_ACTION_LABELS: Record<ImportStyleAction, { label: string; color: string }> = {
  preset: { label: '对应预设', color: 'blue' },
  reuse: { label: '复用已有', color: 'green' },
  create: { label: '新建', color: 'gold' },
  copy: { label: '预设缺失，复制为自定义', color: 'orange' },
}

export function styleActionLabel(action: string): { label: string; color: string } {
  return STYLE_ACTION_LABELS[action as ImportStyleAction] ?? { label: action, color: 'default' }
}

/** 上传前检查（后端仍是最终权威）：扩展名 .json、非空、≤ 100MB。返回错误文案，通过时为 null。 */
export function precheckBackupFile(file: { name: string; size: number }): string | null {
  if (!file.name.toLowerCase().endsWith('.json')) return '请选择 .json 备份文件（ainovel.project）'
  if (file.size === 0) return '文件为空'
  if (file.size > BACKUP_MAX_BYTES) return BACKUP_TOO_LARGE
  return null
}

/** 400 响应体里的 errors（文件内问题，前 20 条）；不是此类错误时为空数组。 */
export function importFileErrors(err: unknown): string[] {
  if (!(err instanceof ApiError) || err.status !== 400 || !err.body) return []
  const raw = err.body.errors
  if (!Array.isArray(raw)) return []
  return raw.filter((e): e is string => typeof e === 'string' && e.trim() !== '')
}

/** 导入令牌过期 / 失效（需要重新选择文件） */
export function isTokenExpired(err: unknown): boolean {
  return err instanceof ApiError && err.status === 400 && err.detail.includes(TOKEN_EXPIRED_TEXT)
}

/** 本地记录的令牌截止时间是否已过（留 5 秒余量，避免点击时恰好过期）。 */
export function tokenExpiredLocally(expiresAt: number, now = Date.now()): boolean {
  return now >= expiresAt - 5000
}
