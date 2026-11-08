import { describe, expect, it } from 'vitest'
import { reviewTextareaOffset, sourceToTextareaOffset, validReviewAnchor } from './reviewOffsets'

describe('待核对候选光标', () => {
  it('显式换算原始 CRLF、单独 CR/LF、emoji 与组合字符', () => {
    const source = '甲\r\n🙂e\u0301\r末\n'
    const dom = '甲\n🙂e\u0301\n末\n'
    const expected = new Map([[0, 0], [1, 1], [2, 3], [4, 4], [5, 5], [6, 6], [7, 7], [8, 8], [9, 9]])
    for (const [domOffset, codepoints] of expected) {
      expect(reviewTextareaOffset(source, dom, domOffset)).toBe(codepoints)
      expect(sourceToTextareaOffset(source, codepoints)).toBe(domOffset)
    }
    expect(reviewTextareaOffset(source, dom, 3)).toBeNull()
    expect(validReviewAnchor(source, 2)).toBe(false)
    expect(sourceToTextareaOffset(source, 2)).toBeNull()
    expect(validReviewAnchor(source, 3)).toBe(true)
  })

  it('浏览器值或坐标不一致时拒绝猜测', () => {
    expect(reviewTextareaOffset('甲\r\n乙', '甲乙', 1)).toBeNull()
    expect(reviewTextareaOffset('🙂', '🙂', 1)).toBeNull()
    expect(reviewTextareaOffset('甲', '甲', -1)).toBeNull()
    expect(validReviewAnchor('甲', 2)).toBe(false)
  })
})
