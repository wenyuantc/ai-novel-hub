import { afterEach, describe, expect, it, vi } from 'vitest'
import { cardApi, cardFilename, defaultResolutions, importedName, MAX_CARD_BYTES, parseCardText, summarizeImport } from './cards'
import type { CardPreview } from './types'

const preview: CardPreview = {
  fields_revision: 0, categories_revision: 0, can_import: true, field_definitions: [], references: [],
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
afterEach(() => vi.unstubAllGlobals())

describe('卡片导入', () => {
  it('v5确认明确传递原预览的字段与分类双版本，服务端冲突原样透传', async () => {
    const calls: RequestInit[] = []
    vi.stubGlobal('fetch', async (_url: string, init: RequestInit) => { calls.push(init); return new Response(JSON.stringify({ detail: '整理标签已变化', code: 'stale_revision', scope: 'categories', current_revision: 9 }), { status: 409 }) })
    await expect(cardApi.import(7, { version: 5 }, { characters: {}, organizations: {} }, { expected_fields_revision: 2, expected_categories_revision: 3 })).rejects.toMatchObject({ status: 409, body: { scope: 'categories', current_revision: 9 } })
    expect(JSON.parse(String(calls[0].body))).toEqual({ data: { version: 5 }, resolutions: { characters: {}, organizations: {} }, expected_fields_revision: 2, expected_categories_revision: 3 })
  })
  it('v4预览与确认携带全部显式映射和预览版本，旧调用仍接受原数据', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => { calls.push({ url, init }); return new Response('{}') })
    const data = { format: 'ainovel.cards', version: 4 }
    const resolutions = { characters: { 林远: 'skip' as const }, organizations: {} }
    const mappings = { references: { outside: { kind: 'world' as const, id: 21 } }, definitions: { rank: { action: 'reuse' as const, target_definition_id: 13, option_mappings: { low: 17 } } } }
    const signal = new AbortController().signal
    await cardApi.preview(7, data, { resolutions, mappings }, signal)
    await cardApi.import(7, data, resolutions, { mappings, expected_fields_revision: 5 }, signal)
    await cardApi.preview(7, { version: 3 })
    expect(calls.map(({ url }) => url)).toEqual(['/api/v1/projects/7/cards/import/preview', '/api/v1/projects/7/cards/import', '/api/v1/projects/7/cards/import/preview'])
    expect(calls.slice(0, 2).every(({ init }) => init.credentials === 'include' && init.signal === signal)).toBe(true)
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ data, resolutions, mappings })
    expect(JSON.parse(String(calls[1].init.body))).toEqual({ data, resolutions, mappings, expected_fields_revision: 5 })
    expect(JSON.parse(String(calls[2].init.body))).toEqual({ data: { version: 3 } })
  })
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
