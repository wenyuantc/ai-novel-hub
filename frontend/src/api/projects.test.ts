import { describe, expect, it } from 'vitest'
import { decodeExportWarnings, exportQuery, parseContentDispositionFilename } from './projects'
import { backupQuery } from './transfer'

describe('Content-Disposition 文件名', () => {
  it('优先 RFC 5987 的 filename*', () => {
    const name = '星河尽头-备份-20261027.ainovel.json'
    const header = `attachment; filename="backup.ainovel.json"; filename*=UTF-8''${encodeURIComponent(name)}`
    expect(parseContentDispositionFilename(header)).toBe(name)
  })
  it('没有 filename* 时取 filename；没有头为 null', () => {
    expect(parseContentDispositionFilename('attachment; filename="novel.epub"')).toBe('novel.epub')
    expect(parseContentDispositionFilename(null)).toBeNull()
  })
  it('filename* 解码失败时退回 filename', () => {
    expect(parseContentDispositionFilename(`attachment; filename="a.txt"; filename*=UTF-8''%E4%ZZ`)).toBe('a.txt')
  })
})

describe('X-Export-Warnings 解码', () => {
  it('百分号编码、多条以「；」分隔', () => {
    const header = encodeURIComponent('第3章「空章」正文为空，已跳过；第5章「x」正文为空，已跳过')
    expect(decodeExportWarnings(header)).toEqual(['第3章「空章」正文为空，已跳过', '第5章「x」正文为空，已跳过'])
  })
  it('空 / 缺失为空数组，非法编码按原文', () => {
    expect(decodeExportWarnings(null)).toEqual([])
    expect(decodeExportWarnings('')).toEqual([])
    expect(decodeExportWarnings('100%完成')).toEqual(['100%完成'])
  })
})

describe('导出查询参数', () => {
  it('txt / md 不带 EPUB 参数', () => {
    expect(exportQuery('txt', { chapterIds: [3, 1], author: 'x', includeSynopsis: false })).toEqual({
      format: 'txt',
      chapter_ids: '3,1',
    })
    expect(exportQuery('md')).toEqual({ format: 'md' })
  })
  it('epub 带作者与简介页开关（默认包含）', () => {
    expect(exportQuery('epub', { author: '' })).toEqual({ format: 'epub', author: '', include_synopsis: 'true' })
    expect(exportQuery('epub', { chapterIds: [], author: '林', includeSynopsis: false })).toEqual({
      format: 'epub',
      author: '林',
      include_synopsis: 'false',
    })
  })
})

describe('备份查询参数', () => {
  it('generation_logs 映射为 logs', () => {
    expect(backupQuery({ versions: true, snapshots: false, generation_logs: true, inspiration: false })).toEqual({
      versions: true,
      snapshots: false,
      logs: true,
      inspiration: false,
    })
  })
})
