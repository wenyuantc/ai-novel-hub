// 知识库与剧情记忆的纯函数：状态文案、上传前检查、分数展示、索引横幅、记忆类型标签

import { ApiError, errorMessage } from '../api/client'
import { COLLABORATOR_MEMORY_RETRY_HINT } from './projectAccess'
import type {
  IndexKeywordReason, IndexStatus, KbDocument, KbDocumentStatus, MemoryKind, MemoryOrigin, SearchMode,
} from '../api/types'

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
  /** 显示置灰的「重建索引」按钮并以此说明原因（作者本人向量配置不可用，E6-f 裁决 18） */
  rebuildDisabledReason?: string
  /** 作者视图附「去模型配置」链接 */
  configLink?: boolean
}

/** 作者本人向量配置存在但本地不可用（解密失败、端点无效等；index-status keyword_reason=owner_embedding_unavailable）。 */
export const OWNER_EMBEDDING_UNAVAILABLE = '向量配置不可用，请到模型配置检查'
export const OWNER_EMBEDDING_UNAVAILABLE_REBUILD = '向量配置不可用，修复模型配置后才能重建索引'

/** 作者视图：服务端报告本人向量配置不可用（仅作者自己的配置，可提示去模型配置）。 */
export function ownerEmbeddingUnavailable(status: IndexStatus | undefined): boolean {
  return status?.keyword_reason === 'owner_embedding_unavailable'
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
      configLink: true,
    }
  }
  if (ownerEmbeddingUnavailable(status)) {
    return {
      type: 'warning',
      message: OWNER_EMBEDDING_UNAVAILABLE,
      description: `当前只用关键词检索${status.stale_count > 0 ? `，${status.stale_count} 条资料待建向量` : ''}。` +
        '检查向量模型的密钥、端点等配置，修复后可重建索引。',
      canRebuild: false,
      rebuildDisabledReason: OWNER_EMBEDDING_UNAVAILABLE_REBUILD,
      configLink: true,
    }
  }
  if (status.needs_rebuild) {
    const others = scope === 'kb' ? status.stale.memory : status.stale.kb
    const otherWhat = scope === 'kb' ? '剧情记忆' : '知识库分块'
    return {
      type: 'warning',
      message: `需要重建索引：${status.stale_count} 条资料待建向量`,
      description:
        `有 ${n} 条${what}${others > 0 ? `、${others} 条${otherWhat}` : ''}缺少当前向量模型（${status.embedding_model}）的向量` +
        '（更换了向量模型或之前生成失败）。重建完成前只用关键词检索。',
      canRebuild: true,
    }
  }
  return null
}

// ---------------- 协作者视图（E6-e）：一律以服务端 index-status 为准，前端不自行推算授权 ----------------

/** 关键词降级原因（协作者可见，不含作者配置细节） */
export const KEYWORD_REASON_TEXT: Record<IndexKeywordReason, string> = {
  owner_embedding_not_allowed: '作者未授权使用其模型配置',
  embedding_config_missing: '作者未配置向量模型',
  owner_embedding_unavailable: '作者的向量模型配置当前不可用',
}

/** 未知原因（旧服务 / 新增取值）只给中性文案，不臆断。 */
export function keywordReasonText(reason: IndexKeywordReason | null | undefined): string {
  return (reason && KEYWORD_REASON_TEXT[reason]) || '当前不能使用作者共享的向量模型'
}

export const COLLABORATOR_VECTOR_COST = '使用作者共享的向量模型，费用由作者承担'

export interface CollaboratorKbAccess {
  /** index-status 已读到 */
  known: boolean
  /** 服务端确认本人检索 / 建索引会用作者共享向量：重新索引、全书重建可用 */
  vector: boolean
  /** 作者已授权（未授权时隐藏检索调试） */
  authorized: boolean
  /** 不可用向量时的原因文案；vector 或未知时为 null */
  reason: string | null
}

/** 协作者在知识库 / 记忆页的向量能力，只由服务端 index-status 投影派生。 */
export function collaboratorKbAccess(status: IndexStatus | undefined): CollaboratorKbAccess {
  if (!status) return { known: false, vector: false, authorized: false, reason: null }
  const vector = status.retrieval_mode === 'vector'
  return {
    known: true,
    vector,
    authorized: status.keyword_reason !== 'owner_embedding_not_allowed',
    reason: vector ? null : keywordReasonText(status.keyword_reason),
  }
}

/** Agent 会话：文本模型回退作者授权模型时的费用提示（E4i-f）。 */
export const AGENT_OWNER_MODEL_COST = '本会话的模型调用及确认启动的任务将使用作者授权的模型，外部服务费用由作者账户承担。'
/** Agent 会话：协作者用本人文本模型、但检索使用作者共享向量时的费用提示（裁决 31）。 */
export const AGENT_OWNER_VECTOR_COST = '知识库检索将使用作者共享的向量模型，向量费用由作者承担。'
/** 两者同时成立时合并为一行，避免重复。 */
export const AGENT_OWNER_MODEL_VECTOR_COST = '本会话的模型调用、知识库检索向量及确认启动的任务将使用作者授权的模型，外部服务费用由作者账户承担。'

/**
 * Agent 输入区的作者费用提示（裁决 31）：检索是否用作者向量只由服务端 index-status 投影决定
 * （`collaboratorKbAccess(status).vector`），状态未知 / 读取失败不臆断；作者本人（collaborator=false）不提示向量费用。
 */
export function agentOwnerCostNotice(input: { collaborator: boolean; modelSource: string; status: IndexStatus | undefined }): string | null {
  const vector = input.collaborator && collaboratorKbAccess(input.status).vector
  if (input.modelSource === 'owner') return vector ? AGENT_OWNER_MODEL_VECTOR_COST : AGENT_OWNER_MODEL_COST
  return vector ? AGENT_OWNER_VECTOR_COST : null
}

/** 协作者上传区提示：已授权且作者向量可用 → 费用由作者承担；否则仅关键词并说明原因。 */
export function collaboratorUploadHint(status: IndexStatus | undefined): { message: string; description: string } {
  const access = collaboratorKbAccess(status)
  if (!access.known) {
    return { message: '正在确认索引方式', description: '上传后由服务器按作者授权与配置决定使用作者共享的向量模型或仅关键词检索。' }
  }
  if (access.vector) {
    return {
      message: COLLABORATOR_VECTOR_COST,
      description: '上传后会创建建索引任务（计入你的任务并发额度），向量调用使用作者的 API 账户。',
    }
  }
  return {
    message: '仅关键词检索',
    description: `${access.reason}；上传的资料只建立关键词索引，不会调用作者的向量模型，也不会产生作者向量费用。`,
  }
}

/** 协作者检索调试顶部的一行说明。 */
export function collaboratorSearchNotice(status: IndexStatus | undefined): string | null {
  const access = collaboratorKbAccess(status)
  if (!access.known) return null
  return access.vector ? `查询会${COLLABORATOR_VECTOR_COST}。` : `仅关键词检索：${access.reason}。`
}


/**
 * 协作者视图的索引横幅：不显示作者配置名，不出现「去模型配置」；建索引中优先；
 * 待建向量（stale_count > 0）时提示数量，向量可用时允许（经费用确认）全书重建。
 */
export function collaboratorIndexBanner(status: IndexStatus | undefined, scope: 'kb' | 'memory'): IndexBanner | null {
  if (!status) return null
  const access = collaboratorKbAccess(status)
  const pending = status.stale_count > 0 ? `${status.stale_count} 条资料待建向量` : ''
  const memoryNote = scope === 'memory' && status.stale.memory > 0
    ? `你编辑的记忆若建向量失败，${COLLABORATOR_MEMORY_RETRY_HINT}。` : ''
  if (status.indexing) {
    return {
      type: 'info',
      message: '正在建索引',
      description: [pending ? `还有 ${pending}，完成前这部分检索只用关键词。` : '完成前检索只用关键词。', memoryNote].join(''),
      canRebuild: false,
    }
  }
  if (!access.vector) {
    return {
      type: 'info',
      message: `当前只用关键词检索：${access.reason}`,
      description: [pending ? `${pending}。` : '', memoryNote].join('') || undefined,
      canRebuild: false,
    }
  }
  if (status.stale_count > 0) {
    return {
      type: 'warning',
      message: pending,
      description: `补建完成前整书检索只用关键词。全书重建会${COLLABORATOR_VECTOR_COST}。${memoryNote}`,
      canRebuild: true,
    }
  }
  return null
}

/** 协作者看到「仅关键词」文档时的说明。 */
export function collaboratorDocStatusHint(doc: Pick<KbDocument, 'status' | 'error'>, status: IndexStatus | undefined): string {
  if (doc.status !== 'ready_keyword_only') return docStatusHint(doc)
  return collaboratorKbAccess(status).vector
    ? `已分块，当前仅使用关键词检索；可点「重建索引」补建向量（${COLLABORATOR_VECTOR_COST}）。`
    : '已分块，当前仅使用关键词检索；如需向量索引，请联系作者。'
}

/** 知识库向量操作的错误文案：作者未授权 403 给友好说明，其余沿用后端 detail。 */
export function kbActionError(err: unknown): string {
  if (err instanceof ApiError && err.status === 403 && err.body?.code === 'owner_embedding_not_allowed') {
    return '作者未授权使用其模型配置，不能建立向量索引；当前仅使用关键词检索。'
  }
  return errorMessage(err)
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

/** 仅有证据时标示自动记忆来源；旧服务未提供字段时不臆断。 */
export function memorySourceStatus(m: {
  origin: MemoryOrigin
  source_stale?: boolean | null
}): 'stale' | 'unknown' | null {
  if (m.origin !== 'auto') return null
  if (m.source_stale === true) return 'stale'
  if (m.source_stale === null) return 'unknown'
  return null
}

/** 章号范围筛选：起止都给出且起 > 止时互换 */
export function normalizeChapterRange(from: number | null | undefined, to: number | null | undefined): [number?, number?] {
  const a = from ?? undefined
  const b = to ?? undefined
  if (a !== undefined && b !== undefined && a > b) return [b, a]
  return [a, b]
}
