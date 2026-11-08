import { describe, expect, it } from 'vitest'
import type { RichDocument, RichSection } from '../../api/richDocument'
import { richDocumentCapacityProblem, richDocumentSaveError } from './richDocumentCapacity'

const formatting = () => ({ version: 1 as const, boundaries: [], inline: [], blocks: [] })
const document = (): RichDocument => ({ content: '正文', formatting: formatting(), sections: [], retired_section_keys: [], markers: [] })
const section = (key: string, content: string): RichSection => ({ key, anchor: 0, order: Number(key) || 0,
  title: '区块', content, formatting: formatting(), include_in_ai: false })

describe('富文档保存容量提示', () => {
  it('单区块按 Unicode 码点计算上限，刚好 50 万个 emoji 允许，多一个拒绝', () => {
    const draft = document()
    draft.sections = [section('1', '🙂'.repeat(500_000))]
    expect(richDocumentCapacityProblem(draft)).toBeNull()
    draft.sections[0].content += '字'
    expect(richDocumentCapacityProblem(draft)).toContain('第 1 个区块的正文超过 50 万字符')
  })

  it('区块合计恰好 200 万码点允许，超出明确提示', () => {
    const draft = document()
    draft.sections = Array.from({ length: 4 }, (_, index) => section(String(index), '文'.repeat(500_000)))
    expect(richDocumentCapacityProblem(draft)).toBeNull()
    draft.sections.push(section('4', '🙂'))
    expect(richDocumentCapacityProblem(draft)).toContain('合计超过 200 万字符')
  })

  it('区块、标记及每个格式数组分别按结构上限提示', () => {
    const draft = document()
    draft.sections = Array.from({ length: 1001 }, (_, index) => section(String(index), ''))
    expect(richDocumentCapacityProblem(draft)).toContain('区块超过 1000')
    draft.sections = []
    draft.retired_section_keys = Array.from({ length: 20_001 }, (_, index) => String(index))
    expect(richDocumentCapacityProblem(draft)).toContain('退场区块身份超过 2 万')
    draft.retired_section_keys = []
    draft.markers = Array.from({ length: 20_000 }, (_, index) => ({ key: String(index), section_key: null,
      status: 'stale' as const, start: null, end: null, color: 'yellow' as const, excerpt: '', excerpt_truncated: false, last_start: 0 }))
    expect(richDocumentCapacityProblem(draft)).toBeNull()
    draft.markers.push({ ...draft.markers[0], key: 'extra' })
    expect(richDocumentCapacityProblem(draft)).toContain('标记超过 2 万')
    draft.markers = []
    draft.formatting.boundaries = Array.from({ length: 20_000 }, (_, index) => index)
    expect(richDocumentCapacityProblem(draft)).toBeNull()
    draft.formatting.boundaries.push(20_000)
    expect(richDocumentCapacityProblem(draft)).toContain('正文的段落边界超过 2 万')
    draft.formatting.boundaries = []
    const selected = section('1', '')
    selected.formatting.inline = Array.from({ length: 20_001 }, () => ({ kind: 'bold', start: 0, end: 1 }))
    draft.sections = [selected]
    expect(richDocumentCapacityProblem(draft)).toContain('第 1 个区块的文字样式超过 2 万')
  })

  it('8 MiB 的字节判断留给服务端，并将其超限结果转换成保留草稿提示', () => {
    expect(richDocumentSaveError('请求参数校验失败：document: 文档元数据总量超过上限'))
      .toContain('元数据合计超过 8 MiB')
    expect(richDocumentSaveError('其他错误')).toBe('其他错误')
  })
})
