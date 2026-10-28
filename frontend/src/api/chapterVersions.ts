import { api } from './client'
import { runGeneration } from './sse'
import type {
  Chapter,
  ChapterVersion,
  ChapterVersionBrief,
  ChapterVersionUpdate,
  GenerationKind,
  GenerationLog,
  GenerationLogBrief,
  GenerationStatus,
  RewriteApply,
  RewriteDone,
  RewriteOp,
  RewriteRequest,
  VersionSource,
} from './types'

const base = (pid: number, cid: number) => `/projects/${pid}/chapters/${cid}`

export const versionApi = {
  list: (pid: number, cid: number) => api.get<ChapterVersionBrief[]>(`${base(pid, cid)}/versions`),
  get: (pid: number, cid: number, vid: number) => api.get<ChapterVersion>(`${base(pid, cid)}/versions/${vid}`),
  update: (pid: number, cid: number, vid: number, body: ChapterVersionUpdate) =>
    api.patch<ChapterVersionBrief>(`${base(pid, cid)}/versions/${vid}`, body),
  remove: (pid: number, cid: number, vid: number) => api.del(`${base(pid, cid)}/versions/${vid}`),
  restore: (pid: number, cid: number, vid: number) => api.post<Chapter>(`${base(pid, cid)}/versions/${vid}/restore`),
}

export const generationLogApi = {
  list: (pid: number, cid: number) => api.get<GenerationLogBrief[]>(`${base(pid, cid)}/generation-logs`),
  get: (pid: number, cid: number, lid: number) => api.get<GenerationLog>(`${base(pid, cid)}/generation-logs/${lid}`),
}

/** 局部改写预览（SSE）：只流式返回结果，不改正文。 */
export function rewriteChapter(
  cid: number,
  body: RewriteRequest,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<RewriteDone> {
  return runGeneration<RewriteDone>(`/chapters/${cid}/rewrite`, body, { onDelta, signal })
}

/** 采纳局部改写；原文已变化时后端返回 409。 */
export function applyRewrite(pid: number, cid: number, body: RewriteApply): Promise<Chapter> {
  return api.post<Chapter>(`${base(pid, cid)}/rewrite/apply`, body)
}

export const VERSION_SOURCE: Record<VersionSource, { label: string; color: string }> = {
  manual: { label: '手动保存', color: 'default' },
  baseline: { label: '替换前快照', color: 'gold' },
  generate: { label: 'AI 生成', color: 'blue' },
  polish: { label: 'AI 润色', color: 'cyan' },
  rewrite: { label: '局部改写', color: 'purple' },
  restore: { label: '回滚', color: 'magenta' },
  revise: { label: '按建议重写', color: 'geekblue' },
  import: { label: '导入', color: 'green' },
  agent: { label: '智能体修改', color: 'cyan' },
}

export const GENERATION_KIND_LABELS: Record<GenerationKind, string> = {
  'agent.turn': '项目智能体',
  generate: '整章生成',
  polish: '润色',
  rewrite: '改写',
  expand: '扩写',
  condense: '缩写',
  deslop: '去 AI 味',
  continue: '续写',
  revise: '按建议重写',
  'book_import.digest': '拆书通读',
  'book_import.synthesize': '拆书汇总',
  'book_import.style': '拆书风格提取',
}

export const GENERATION_STATUS: Record<GenerationStatus, { label: string; color: string }> = {
  success: { label: '成功', color: 'success' },
  error: { label: '失败', color: 'error' },
  cancelled: { label: '已取消', color: 'default' },
}

export const REWRITE_OP_LABELS: Record<RewriteOp, string> = {
  rewrite: '改写',
  expand: '扩写',
  condense: '缩写',
  deslop: '去 AI 味',
  continue: '续写',
}
