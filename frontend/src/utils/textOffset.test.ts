import { describe, expect, it } from 'vitest'
import { codePointLength, codePointToUtf16, utf16ToCodePoint } from './textOffset'

describe('textOffset', () => {
  it('纯 BMP 文本两种偏移相同', () => {
    const text = '夜色深沉，星舰缓缓'
    for (let i = 0; i <= text.length; i++) {
      expect(utf16ToCodePoint(text, i)).toBe(i)
      expect(codePointToUtf16(text, i)).toBe(i)
    }
  })

  it('emoji 与 CJK 扩展 B 区字符按一个码点计', () => {
    // 😀 与 𠀀（U+20000）在 UTF-16 中各占 2 个单元
    const text = 'a😀b𠀀c'
    expect(text.length).toBe(7)
    expect(codePointLength(text)).toBe(5)
    expect(utf16ToCodePoint(text, 1)).toBe(1)
    expect(utf16ToCodePoint(text, 3)).toBe(2) // 😀 之后
    expect(utf16ToCodePoint(text, 6)).toBe(4) // 𠀀 之后
    expect(codePointToUtf16(text, 2)).toBe(3)
    expect(codePointToUtf16(text, 4)).toBe(6)
    expect(codePointToUtf16(text, 5)).toBe(7)
  })

  it('往返换算后切片与 Array.from 一致', () => {
    const text = '前文😀𠮷野家🎉后文'
    const cps = Array.from(text)
    for (let s = 0; s <= cps.length; s++) {
      for (let e = s; e <= cps.length; e++) {
        const u16 = text.slice(codePointToUtf16(text, s), codePointToUtf16(text, e))
        expect(u16).toBe(cps.slice(s, e).join(''))
        expect(utf16ToCodePoint(text, codePointToUtf16(text, e))).toBe(e)
      }
    }
  })

  it('落在代理对中间按整个字符计入，越界截断', () => {
    const text = '😀x'
    expect(utf16ToCodePoint(text, 1)).toBe(1)
    expect(utf16ToCodePoint(text, 99)).toBe(2)
    expect(codePointToUtf16(text, 99)).toBe(3)
    expect(utf16ToCodePoint(text, -1)).toBe(0)
  })
})
