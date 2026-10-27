// 知识库与剧情记忆的纯函数：状态文案、上传前检查、分数展示、索引横幅、记忆类型标签

import type { IndexStatus, KbDocument, KbDocumentStatus, MemoryKind, MemoryOrigin, SearchMode } from '../api/types'

/** 与后端 kb_parser.EXTENSIONS 一致 */
export const KB_EXTENSIONS = ['.txt', '.text', '.md', '.markdown', '.json', '.docx', '.epub', '.pdf'] as const
/** Upload 的 accept 属性 */
export const KB_ACCEPT = KB_EXTENSIONS.join(',')
/** 单个文件上限 20MB（与后端 MAX_FILE_BYTES 一致） */
export const KB_MAX_FILE_BYTES = 20 * 1024 * 1024
/** 每个项目最多 200 个文档 */
export const KB_MAX_DOCUMENTS = 200

export const KB_DOC_STATUS: Record<KbDocumentStatus, { label: string; color: string }> = {
  pending: { label: '排队中', color: 'default' },
  parsing: { label: '解析中', color: 'processing' },
  indexing: { label: '建索引中', color: 'processing' },
  ready: { label: '就绪', color: 'success' },
  ready_keyword_only: { label: '仅关键词', color: 'gold' },
  failed: { label: '失败', color: 'error' },
}

/** 文档仍在处理（需要轮询刷新） */
export function isDocBusy(status: KbDocumentStatus): boolean {
  return status === 'pending' || status === 'parsing' || status === 'indexing'
}

/** 状态的补充说明（Tag 的 tooltip） */
export function docStatusHint(doc: Pick<KbDocument, 'status' | 'error'>): string {
  switch (doc.status) {
    case 'ready_keyword_only':
      return '已分块，但还没有当前向量模型的向量，检索时只用关键词。配置向量模型后点「重建索引」'
    case 'failed':
      return doc.error || '处理失败，可以重新建索引'
    case 'pending':
      return '等待建索引任务执行'
    case 'indexing':
      return '正在分块并生成向量'
    default:
      return ''
  }
}

function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i < 0 ? '' : name.slice(i).toLowerCase()
}

/**
 * 上传前检查（后端仍是最终权威）：扩展名、大小 ≤ 20MB、文档数（已有 + 本批前面已接受的）< 200。
 * 返回错误文案，通过时为 null。
 */
export function precheckUpload(file: { name: string; size: number }, existingCount: number): string | null {
  if (!(KB_EXTENSIONS as readonly string[]).includes(extOf(file.name))) {
    return `《${file.name}》：不支持的文件类型，仅支持 txt、md、json、docx、epub、pdf`
  }
  if (file.size > KB_MAX_FILE_BYTES) return `《${file.name}》：文件过大，单个文件上限为 20MB`
  if (file.size === 0) return `《${file.name}》：文件为空`
  if (existingCount >= KB_MAX_DOCUMENTS) {
    return `每个项目最多 ${KB_MAX_DOCUMENTS} 个文档，请先删除不需要的文档`
  }
  return null
}

/** 文件大小展示 */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 分数（0–1）保留两位；null（只用关键词时的向量分）为「—」 */
export function formatScore(score: number | null | undefined): string {
  if (score === null || score === undefined || !Number.isFinite(score)) return '—'
  return Math.min(1, Math.max(0, score)).toFixed(2)
}

/** 分数颜色：≥0.7 绿、≥0.4 蓝、其余灰 */
export function scoreColor(score: number): string {
  if (score >= 0.7) return 'green'
  if (score >= 0.4) return 'blue'
  return 'default'
}

export const SEARCH_MODE_LABELS: Record<SearchMode, string> = {
  hybrid: '混合检索（向量 70% + 关键词 30%）',
  keyword: '仅关键词检索',
}

/** 检索调试结果的说明行 */
export function searchSummary(r: { mode: SearchMode; candidates: number; hits: unknown[] }): string {
  return `${SEARCH_MODE_LABELS[r.mode]} · 候选 ${r.candidates} 条 · 命中 ${r.hits.length} 条`
}

export interface IndexBanner {
  type: 'info' | 'warning'
  message: string
  description?: string
  /** 显示「重建索引」按钮 */
  canRebuild: boolean
}

/**
 * 索引状态横幅：建索引中（info，不能重建）优先于需要重建（warning）；没有向量模型时提示只用关键词。
 * scope 决定提到哪一类内容的数量（知识库页 = kb，记忆页 = memory）。都正常时为 null。
 */
export function indexBanner(status: IndexStatus | undefined, scope: 'kb' | 'memory'): IndexBanner | null {
  if (!status) return null
  const what = scope === 'kb' ? '知识库分块' : '剧情记忆'
  const n = status.stale[scope]
  if (status.indexing) {
    return {
      type: 'info',
      message: '正在建索引',
      description:
        n > 0 ? `还有 ${n} 条${what}没有当前向量模型的向量，完成前这部分检索只用关键词。` : '完成前检索只用关键词。',
      canRebuild: false,
    }
  }
  if (status.embedding_model === null) {
    return {
      type: 'info',
      message: '未配置向量模型，当前只用关键词检索',
      description: '在「模型配置」中添加向量模型后可启用语义检索（混合排序）。',
      canRebuild: false,
    }
  }
  if (status.needs_rebuild) {
    const others = scope === 'kb' ? status.stale.memory : status.stale.kb
    const otherWhat = scope === 'kb' ? '剧情记忆' : '知识库分块'
    return {
      type: 'warning',
      message: '需要重建索引',
      description:
        `有 ${n} 条${what}${others > 0 ? `、${others} 条${otherWhat}` : ''}缺少当前向量模型（${status.embedding_model}）的向量` +
        '（更换了向量模型或之前生成失败）。重建完成前只用关键词检索。',
      canRebuild: true,
    }
  }
  return null
}

export const MEMORY_KIND_LABELS: Record<MemoryKind, string> = {
  summary: '摘要',
  plot_point: '情节',
  hook: '钩子',
  character: '人物',
  foreshadow: '伏笔',
  manual: '备忘',
}

export const MEMORY_KIND_COLORS: Record<MemoryKind, string> = {
  summary: 'blue',
  plot_point: 'geekblue',
  hook: 'volcano',
  character: 'green',
  foreshadow: 'purple',
  manual: 'gold',
}

export const MEMORY_ORIGIN_LABELS: Record<MemoryOrigin, string> = {
  auto: '自动',
  manual: '手动',
}

/** 记忆操作权限：自动记忆只能钉住 / 屏蔽，手动记忆可以编辑和删除 */
export function memoryEditable(m: { origin: MemoryOrigin }): boolean {
  return m.origin === 'manual'
}

/** 章号范围筛选：起止都给出且起 > 止时互换 */
export function normalizeChapterRange(from: number | null | undefined, to: number | null | undefined): [number?, number?] {
  const a = from ?? undefined
  const b = to ?? undefined
  if (a !== undefined && b !== undefined && a > b) return [b, a]
  return [a, b]
}
