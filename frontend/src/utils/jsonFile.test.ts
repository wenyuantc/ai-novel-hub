import { describe, expect, it } from 'vitest'
import { datedFilename, parseJsonFileText } from './jsonFile'

describe('parseJsonFileText', () => {
  it('合法 JSON', () => {
    expect(parseJsonFileText('{"a": 1}', 100, '过大')).toEqual({ data: { a: 1 }, error: null })
  })
  it('非 JSON', () => {
    expect(parseJsonFileText('{', 100, '过大').error).toBe('文件不是有效的 JSON')
  })
  it('按紧凑序列化后的 UTF-8 字节数判定大小（缩进不计）', () => {
    const pretty = JSON.stringify({ s: '中文' }, null, 8)
    // 紧凑：{"s":"中文"} = 8 + 6 字节
    expect(parseJsonFileText(pretty, 14, '过大').error).toBeNull()
    expect(parseJsonFileText(pretty, 13, '过大').error).toBe('过大')
  })
})

it('datedFilename', () => {
  expect(datedFilename('ainovel-写作风格', new Date(2026, 9, 6))).toBe('ainovel-写作风格-20261006.json')
})
