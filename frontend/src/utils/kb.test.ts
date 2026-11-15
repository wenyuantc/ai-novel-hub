import { ApiError } from '../api/client'
import { describe, expect, it } from 'vitest'
import type { IndexStatus, KbDocumentStatus, MemoryKind } from '../api/types'
import { currentIndexStatus, indexStatusKey } from '../hooks/useIndexStatus'
import {
  AGENT_OWNER_MODEL_COST,
  AGENT_OWNER_MODEL_VECTOR_COST,
  AGENT_OWNER_VECTOR_COST,
  agentOwnerCostNotice,
  docStatusHint,
  formatFileSize,
  formatScore,
  indexBanner,
  collaboratorDocStatusHint,
  collaboratorIndexBanner,
  collaboratorKbAccess,
  collaboratorSearchNotice,
  collaboratorUploadHint,
  kbActionError,
  keywordReasonText,
  isDocBusy,
  KB_DOC_STATUS,
  KB_MAX_FILE_BYTES,
  MEMORY_KIND_LABELS,
  memoryEditable,
  memorySourceStatus,
  normalizeChapterRange,
  precheckUpload,
  scoreColor,
  searchSummary,
} from './kb'

function status(over: Partial<IndexStatus> = {}): IndexStatus {
  return {
    embedding_model: '向量 · bge-m3',
    needs_rebuild: false,
    stale_count: 0,
    stale: { kb: 0, memory: 0 },
    indexing: false,
    owner_embedding_configured: true,
    retrieval_mode: 'vector',
    keyword_reason: null,
    ...over,
  }
}

describe('文档状态文案', () => {
  it('每个状态都有标签，仅关键词单独标出', () => {
    const all: KbDocumentStatus[] = ['pending', 'parsing', 'indexing', 'ready', 'ready_keyword_only', 'failed']
    for (const s of all) expect(KB_DOC_STATUS[s].label).toBeTruthy()
    expect(KB_DOC_STATUS.ready_keyword_only.label).toBe('仅关键词')
    expect(KB_DOC_STATUS.indexing.label).toBe('建索引中')
  })

  it('处理中的状态需要轮询', () => {
    expect(isDocBusy('pending')).toBe(true)
    expect(isDocBusy('parsing')).toBe(true)
    expect(isDocBusy('indexing')).toBe(true)
    expect(isDocBusy('ready')).toBe(false)
    expect(isDocBusy('ready_keyword_only')).toBe(false)
    expect(isDocBusy('failed')).toBe(false)
  })

  it('失败提示优先用后端 error，就绪无提示', () => {
    expect(docStatusHint({ status: 'failed', error: '模型调用失败：额度不足' })).toBe('模型调用失败：额度不足')
    expect(docStatusHint({ status: 'failed', error: '' })).toContain('重新建索引')
    expect(docStatusHint({ status: 'ready_keyword_only', error: '' })).toContain('只用关键词')
    expect(docStatusHint({ status: 'ready', error: '' })).toBe('')
  })
})

describe('上传前检查', () => {
  it('支持 6 种格式（含 .markdown / .text，大小写不敏感）', () => {
    for (const name of ['a.txt', 'a.text', 'b.md', 'b.MARKDOWN', 'c.json', 'd.docx', 'e.epub', 'f.PDF']) {
      expect(precheckUpload({ name, size: 10 }, 0)).toBeNull()
    }
  })

  it('不支持的类型 / 没有扩展名', () => {
    expect(precheckUpload({ name: 'a.doc', size: 10 }, 0)).toContain('不支持的文件类型')
    expect(precheckUpload({ name: 'README', size: 10 }, 0)).toContain('不支持的文件类型')
  })

  it('20MB 边界与空文件', () => {
    expect(precheckUpload({ name: 'a.pdf', size: KB_MAX_FILE_BYTES }, 0)).toBeNull()
    expect(precheckUpload({ name: 'a.pdf', size: KB_MAX_FILE_BYTES + 1 }, 0)).toContain('20MB')
    expect(precheckUpload({ name: 'a.txt', size: 0 }, 0)).toContain('为空')
  })

  it('文档数达到 200 时拒绝', () => {
    expect(precheckUpload({ name: 'a.txt', size: 1 }, 199)).toBeNull()
    expect(precheckUpload({ name: 'a.txt', size: 1 }, 200)).toContain('最多 200 个文档')
  })
})

describe('分数与大小展示', () => {
  it('两位小数，越界夹到 0–1，null 为「—」', () => {
    expect(formatScore(0.8234)).toBe('0.82')
    expect(formatScore(1)).toBe('1.00')
    expect(formatScore(0)).toBe('0.00')
    expect(formatScore(1.2)).toBe('1.00')
    expect(formatScore(-0.1)).toBe('0.00')
    expect(formatScore(null)).toBe('—')
    expect(formatScore(undefined)).toBe('—')
    expect(formatScore(Number.NaN)).toBe('—')
  })

  it('分数颜色分档', () => {
    expect(scoreColor(0.7)).toBe('green')
    expect(scoreColor(0.5)).toBe('blue')
    expect(scoreColor(0.39)).toBe('default')
  })

  it('文件大小', () => {
    expect(formatFileSize(512)).toBe('512 B')
    expect(formatFileSize(2048)).toBe('2.0 KB')
    expect(formatFileSize(5 * 1024 * 1024)).toBe('5.0 MB')
  })

  it('检索结果说明区分混合与仅关键词', () => {
    expect(searchSummary({ mode: 'hybrid', candidates: 10, hits: [1, 2] })).toBe(
      '混合检索（向量 70% + 关键词 30%） · 候选 10 条 · 命中 2 条',
    )
    expect(searchSummary({ mode: 'keyword', candidates: 3, hits: [] })).toContain('仅关键词')
  })
})

describe('索引横幅', () => {
  it('一切正常时不显示', () => {
    expect(indexBanner(status(), 'kb')).toBeNull()
    expect(indexBanner(undefined, 'kb')).toBeNull()
  })

  it('建索引中优先于需要重建，且不能重建', () => {
    const b = indexBanner(status({ indexing: true, needs_rebuild: true, stale: { kb: 5, memory: 0 } }), 'kb')
    expect(b?.message).toBe('正在建索引')
    expect(b?.canRebuild).toBe(false)
    expect(b?.description).toContain('5 条知识库分块')
  })

  it('需要重建：带模型名与各类数量，可以重建', () => {
    const b = indexBanner(status({ needs_rebuild: true, stale_count: 7, stale: { kb: 4, memory: 3 } }), 'memory')
    expect(b?.type).toBe('warning')
    expect(b?.canRebuild).toBe(true)
    expect(b?.description).toContain('3 条剧情记忆、4 条知识库分块')
    expect(b?.description).toContain('向量 · bge-m3')
  })

  it('需要重建的标题带待建向量数（作者与协作者同口径）', () => {
    expect(indexBanner(status({ needs_rebuild: true, stale_count: 7, stale: { kb: 4, memory: 3 } }), 'kb')?.message)
      .toBe('需要重建索引：7 条资料待建向量')
  })

  it('没有向量模型：只用关键词，不能重建', () => {
    const b = indexBanner(status({ embedding_model: null }), 'kb')
    expect(b?.type).toBe('info')
    expect(b?.message).toContain('只用关键词')
    expect(b?.canRebuild).toBe(false)
  })

  it('作者本人向量配置不可用（E6-f 裁决 18）：提示去模型配置，重建置灰并说明原因；优先于需要重建', () => {
    const b = indexBanner(status({ needs_rebuild: true, stale_count: 7, stale: { kb: 4, memory: 3 },
      retrieval_mode: 'keyword', keyword_reason: 'owner_embedding_unavailable' }), 'kb')
    expect(b?.message).toBe('向量配置不可用，请到模型配置检查')
    expect(b?.canRebuild).toBe(false)
    expect(b?.rebuildDisabledReason).toContain('修复模型配置')
    expect(b?.configLink).toBe(true)
    expect(b?.description).toContain('7 条资料待建向量')
    // 建索引中仍优先；其他关键词原因（作者视图不会出现）不触发
    expect(indexBanner(status({ indexing: true, keyword_reason: 'owner_embedding_unavailable' }), 'kb')?.message)
      .toBe('正在建索引')
    expect(indexBanner(status({ needs_rebuild: true, stale_count: 1, keyword_reason: 'embedding_config_missing' }), 'kb')
      ?.message).toBe('需要重建索引：1 条资料待建向量')
  })
})

describe('记忆', () => {
  it('类型标签与后端召回行一致', () => {
    const kinds: MemoryKind[] = ['summary', 'plot_point', 'hook', 'character', 'foreshadow', 'manual']
    expect(kinds.map((k) => MEMORY_KIND_LABELS[k])).toEqual(['摘要', '情节', '钩子', '人物', '伏笔', '备忘'])
  })

  it('只有手动记忆可以编辑 / 删除', () => {
    expect(memoryEditable({ origin: 'manual' })).toBe(true)
    expect(memoryEditable({ origin: 'auto' })).toBe(false)
  })

  it('只对有证据的自动记忆标注来源过期，不把手动或未知来源伪称新鲜', () => {
    expect(memorySourceStatus({ origin: 'auto', source_stale: true })).toBe('stale')
    expect(memorySourceStatus({ origin: 'auto', source_stale: false })).toBe(null)
    expect(memorySourceStatus({ origin: 'auto', source_stale: null })).toBe('unknown')
    expect(memorySourceStatus({ origin: 'auto' })).toBe(null)
    expect(memorySourceStatus({ origin: 'manual', source_stale: true })).toBe(null)
  })

  it('章号范围：起 > 止时互换，缺省保留 undefined', () => {
    expect(normalizeChapterRange(5, 2)).toEqual([2, 5])
    expect(normalizeChapterRange(2, 5)).toEqual([2, 5])
    expect(normalizeChapterRange(null, 3)).toEqual([undefined, 3])
    expect(normalizeChapterRange(undefined, undefined)).toEqual([undefined, undefined])
  })
})

describe('协作者视图（以服务端 index-status 为准）', () => {
  const collab = (over: Partial<IndexStatus> = {}) => status({ embedding_model: null, ...over })

  it('能力只由 retrieval_mode / keyword_reason 派生；未读到状态时不臆断', () => {
    expect(collaboratorKbAccess(undefined)).toEqual({ known: false, vector: false, authorized: false, reason: null })
    expect(collaboratorKbAccess(collab())).toEqual({ known: true, vector: true, authorized: true, reason: null })
    expect(collaboratorKbAccess(collab({ retrieval_mode: 'keyword', keyword_reason: 'owner_embedding_not_allowed' })))
      .toEqual({ known: true, vector: false, authorized: false, reason: '作者未授权使用其模型配置' })
    expect(collaboratorKbAccess(collab({ retrieval_mode: 'keyword', keyword_reason: 'owner_embedding_unavailable' })))
      .toMatchObject({ vector: false, authorized: true, reason: '作者的向量模型配置当前不可用' })
    expect(keywordReasonText('embedding_config_missing')).toBe('作者未配置向量模型')
    expect(keywordReasonText(null)).toBe('当前不能使用作者共享的向量模型')
  })

  it('上传提示：向量可用 → 费用由作者承担；否则仅关键词 + 原因', () => {
    expect(collaboratorUploadHint(collab()).message).toBe('使用作者共享的向量模型，费用由作者承担')
    const keyword = collaboratorUploadHint(collab({ retrieval_mode: 'keyword', keyword_reason: 'embedding_config_missing' }))
    expect(keyword.message).toBe('仅关键词检索')
    expect(keyword.description).toContain('作者未配置向量模型')
    expect(collaboratorUploadHint(undefined).message).toBe('正在确认索引方式')
    expect(collaboratorSearchNotice(collab())).toContain('费用由作者承担')
    expect(collaboratorSearchNotice(collab({ retrieval_mode: 'keyword', keyword_reason: 'owner_embedding_unavailable' })))
      .toBe('仅关键词检索：作者的向量模型配置当前不可用。')
    expect(collaboratorSearchNotice(undefined)).toBeNull()
  })

  it('横幅：embedding_model 为 null 不显示「未配置」；待建向量数；向量可用才可重建；记忆页带补救说明', () => {
    expect(collaboratorIndexBanner(collab(), 'kb')).toBeNull()
    const pending = collaboratorIndexBanner(collab({ stale_count: 4, stale: { kb: 1, memory: 3 } }), 'memory')
    expect(pending).toMatchObject({ type: 'warning', message: '4 条资料待建向量', canRebuild: true })
    expect(pending?.description).toContain('费用由作者承担')
    expect(pending?.description).toContain('无法直接重试；可重新编辑此条或由有权限者全书重建')
    expect(collaboratorIndexBanner(collab({ stale_count: 1, stale: { kb: 1, memory: 0 } }), 'kb')?.description)
      .not.toContain('无法直接重试')
    const keyword = collaboratorIndexBanner(collab({ retrieval_mode: 'keyword', keyword_reason: 'owner_embedding_not_allowed',
      stale_count: 2, stale: { kb: 2, memory: 0 } }), 'kb')
    expect(keyword).toMatchObject({ type: 'info', canRebuild: false })
    expect(keyword?.message).toBe('当前只用关键词检索：作者未授权使用其模型配置')
    expect(keyword?.description).toBe('2 条资料待建向量。')
    for (const b of [pending, keyword]) expect(JSON.stringify(b)).not.toContain('未配置向量模型，当前')
    const indexing = collaboratorIndexBanner(collab({ indexing: true, stale_count: 2 }), 'kb')
    expect(indexing).toMatchObject({ message: '正在建索引', canRebuild: false })
  })

  it('文档提示与错误文案', () => {
    const doc = { status: 'ready_keyword_only' as const, error: '' }
    expect(collaboratorDocStatusHint(doc, collab())).toContain('费用由作者承担')
    expect(collaboratorDocStatusHint(doc, collab({ retrieval_mode: 'keyword', keyword_reason: 'owner_embedding_not_allowed' })))
      .toContain('请联系作者')
    expect(collaboratorDocStatusHint({ status: 'failed', error: '失败了' }, collab())).toBe('失败了')
    expect(kbActionError(new ApiError(403, '作者未授权共享向量模型', { code: 'owner_embedding_not_allowed' })))
      .toBe('作者未授权使用其模型配置，不能建立向量索引；当前仅使用关键词检索。')
    expect(kbActionError(new ApiError(403, '当前作品没有执行该操作的权限', { code: 'project_readonly' })))
      .toBe('当前作品没有执行该操作的权限')
  })
})

describe('Agent 输入区的作者费用提示（裁决 31）', () => {
  const collab = (over: Partial<IndexStatus> = {}) => status({ embedding_model: null, ...over })
  const keyword = collab({ retrieval_mode: 'keyword', keyword_reason: 'owner_embedding_not_allowed' })

  it('作者本人不提示向量费用（即使服务端为 vector）', () => {
    expect(agentOwnerCostNotice({ collaborator: false, modelSource: 'own', status: status() })).toBeNull()
  })
  it('协作者 + 向量 + 本人模型 → 向量费用由作者承担', () => {
    expect(agentOwnerCostNotice({ collaborator: true, modelSource: 'own', status: collab() })).toBe(AGENT_OWNER_VECTOR_COST)
    expect(AGENT_OWNER_VECTOR_COST).toBe('知识库检索将使用作者共享的向量模型，向量费用由作者承担。')
  })
  it('协作者 + 关键词 / 状态未知 → 不提示', () => {
    expect(agentOwnerCostNotice({ collaborator: true, modelSource: 'own', status: keyword })).toBeNull()
    expect(agentOwnerCostNotice({ collaborator: true, modelSource: 'own', status: undefined })).toBeNull()
  })
  it('模型来源为作者：向量时合并为一行，否则沿用原模型费用文案', () => {
    expect(agentOwnerCostNotice({ collaborator: true, modelSource: 'owner', status: collab() })).toBe(AGENT_OWNER_MODEL_VECTOR_COST)
    expect(AGENT_OWNER_MODEL_VECTOR_COST).toContain('外部服务费用由作者账户承担')
    expect(agentOwnerCostNotice({ collaborator: true, modelSource: 'owner', status: keyword })).toBe(AGENT_OWNER_MODEL_COST)
    expect(agentOwnerCostNotice({ collaborator: true, modelSource: 'owner', status: undefined })).toBe(AGENT_OWNER_MODEL_COST)
  })
  it('旧作品 / 旧授权轮次的响应与关闭时的状态不生效', () => {
    const loaded = { key: indexStatusKey(3, 'v1:1'), status: collab() }
    const notice = (key: string, enabled = true) =>
      agentOwnerCostNotice({ collaborator: true, modelSource: 'own', status: currentIndexStatus(loaded, key, enabled) })
    expect(notice(indexStatusKey(3, 'v1:1'))).toBe(AGENT_OWNER_VECTOR_COST)
    expect(notice(indexStatusKey(4, 'v1:1'))).toBeNull()
    expect(notice(indexStatusKey(3, 'v2:1'))).toBeNull()
    expect(notice(indexStatusKey(3, 'v1:0'))).toBeNull()
    expect(notice(indexStatusKey(3, 'v1:1'), false)).toBeNull()
  })
})
