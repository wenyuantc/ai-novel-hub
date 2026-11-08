import { describe, expect, it } from 'vitest'
import { validateSectionTitle } from './richSectionNode'

describe('区块名称校验', () => {
  it('按后端 Python 空白语义判断，同时保留未裁剪的有效名称', () => {
    expect(validateSectionTitle('')).toBeTruthy()
    expect(validateSectionTitle(' \t\n\r\u001c\u0085\u00a0\u1680\u200a\u2028\u3000')).toBeTruthy()
    expect(validateSectionTitle('\ufeff')).toBeNull() // Python 不把 BOM 视作空白。
    expect(validateSectionTitle('  名称  ')).toBeNull()
  })

  it('以 Unicode 码点而不是 UTF-16 长度计算上限', () => {
    expect(validateSectionTitle('😀'.repeat(200))).toBeNull()
    expect(validateSectionTitle('😀'.repeat(201))).toBeTruthy()
    expect(validateSectionTitle('e\u0301'.repeat(100))).toBeNull()
    expect(validateSectionTitle('e\u0301'.repeat(100) + 'a')).toBeTruthy()
  })
})
