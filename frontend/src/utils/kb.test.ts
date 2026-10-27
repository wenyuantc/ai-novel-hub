import { describe, expect, it } from 'vitest'
import type { IndexStatus, KbDocumentStatus, MemoryKind } from '../api/types'
import {
  docStatusHint,
  formatFileSize,
  formatScore,
  indexBanner,
  isDocBusy,
  KB_DOC_STATUS,
  KB_MAX_FILE_BYTES,
  MEMORY_KIND_LABELS,
  memoryEditable,
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

  it('没有向量模型：只用关键词，不能重建', () => {
    const b = indexBanner(status({ embedding_model: null }), 'kb')
    expect(b?.type).toBe('info')
    expect(b?.message).toContain('只用关键词')
    expect(b?.canRebuild).toBe(false)
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

  it('章号范围：起 > 止时互换，缺省保留 undefined', () => {
    expect(normalizeChapterRange(5, 2)).toEqual([2, 5])
    expect(normalizeChapterRange(2, 5)).toEqual([2, 5])
    expect(normalizeChapterRange(null, 3)).toEqual([undefined, 3])
    expect(normalizeChapterRange(undefined, undefined)).toEqual([undefined, undefined])
  })
})
