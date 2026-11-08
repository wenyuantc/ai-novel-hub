/** 只读 textarea 会规范化 CRLF/CR；将其可见光标映回未修改源文本的 Unicode 码点。 */
export function reviewTextareaOffset(source: string, rendered: string, utf16Offset: number): number | null {
  if (rendered !== source.replace(/\r\n?/g, '\n') || !Number.isInteger(utf16Offset) || utf16Offset < 0 || utf16Offset > rendered.length) return null
  let sourceIndex = 0
  let codepoints = 0
  let domIndex = 0
  while (sourceIndex < source.length) {
    if (domIndex === utf16Offset) return codepoints
    const code = source.codePointAt(sourceIndex)
    if (code === undefined) return null
    if (code === 13 && source[sourceIndex + 1] === '\n') {
      sourceIndex += 2; codepoints += 2; domIndex++
    } else {
      const width = code > 0xffff ? 2 : 1
      sourceIndex += width; codepoints++; domIndex += width
    }
    if (domIndex > utf16Offset) return null // 不能落入 UTF-16 代理对。
  }
  return domIndex === utf16Offset ? codepoints : null
}

export function validReviewAnchor(text: string, offset: number): boolean {
  if (!Number.isSafeInteger(offset) || offset < 0) return false
  const points = Array.from(text)
  return offset <= points.length && !(offset > 0 && offset < points.length && points[offset - 1] === '\r' && points[offset] === '\n')
}

/** 由未修改源文本码点转换为 textarea 对换行规范化后的 UTF-16 光标。 */
export function sourceToTextareaOffset(source: string, offset: number): number | null {
  if (!validReviewAnchor(source, offset)) return null
  return Array.from(source).slice(0, offset).join('').replace(/\r\n?/g, '\n').length
}
