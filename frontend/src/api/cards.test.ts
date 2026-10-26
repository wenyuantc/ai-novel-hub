import { describe, expect, it } from 'vitest'
import { cardFilename, defaultResolutions, importedName, MAX_CARD_BYTES, parseCardText, summarizeImport } from './cards'
import type { CardPreview } from './types'

const preview: CardPreview = {
  characters: [
    { name: '林远', status: 'conflict', rename_to: '林远（导入）' },
    { name: '张三', status: 'new', rename_to: null },
    { name: '李四', status: 'conflict', rename_to: '李四（导入2）' },
  ],
  organizations: [{ name: '天机阁', status: 'new', rename_to: null }],
  relationships: [],
  careers: [{ name: '剑修', status: 'reuse', stages_added: ['渡劫'] }],
  warnings: [],
}

describe('卡片导入', () => {
  it('解析：超限与非 JSON 在本地拦截', () => {
    expect(parseCardText('{"format":"ainovel.cards"}')).toEqual({ data: { format: 'ainovel.cards' }, error: null })
    expect(parseCardText('{oops').error).toBe('文件不是有效的 JSON')
    // 中文按 UTF-8 字节计（3 字节 / 字）
    expect(parseCardText('"' + '中'.repeat(Math.ceil(MAX_CARD_BYTES / 3)) + '"').error).toBe('卡片文件过大，上限为 2MB')
    // 按紧凑序列化计：缩进 / 空白不计入（与后端口径一致）
    const padded = '{"a":' + ' '.repeat(MAX_CARD_BYTES) + '1}'
    expect(parseCardText(padded)).toEqual({ data: { a: 1 }, error: null })
  })

  it('冲突默认跳过，统计与最终名称', () => {
    const r = defaultResolutions(preview)
    expect(r).toEqual({ characters: { 林远: 'skip', 李四: 'skip' }, organizations: {} })
    const choices = { ...r.characters, 李四: 'rename' as const }
    expect(summarizeImport(preview.characters, choices)).toEqual({ created: 1, renamed: 1, skipped: 1 })
    expect(importedName(preview.characters[2], 'rename')).toBe('李四（导入2）')
    expect(importedName(preview.characters[0], 'skip')).toBe('林远')
    expect(importedName(preview.characters[1], 'rename')).toBe('张三')
  })

  it('导出文件名去掉非法字符', () => {
    expect(cardFilename('剑/来:*', new Date(2026, 9, 23))).toBe('剑_来__-卡片-20261023.json')
    expect(cardFilename('  ', new Date(2026, 0, 2))).toBe('ainovel-卡片-20260102.json')
  })
})
