// TextArea 的 selectionStart/End 是 UTF-16 偏移，后端（Python str）按码点计；两者在 emoji、CJK 扩展 B 区等
// 代理对字符处会不同，向后端发送/接收偏移时必须换算。

function isHighSurrogate(code: number): boolean {
  return code >= 0xd800 && code <= 0xdbff
}

function isLowSurrogate(code: number): boolean {
  return code >= 0xdc00 && code <= 0xdfff
}

/** UTF-16 偏移 → 码点偏移；落在代理对中间时按整个字符计入（向后取整）。 */
export function utf16ToCodePoint(text: string, index: number): number {
  const end = Math.max(0, Math.min(index, text.length))
  let cp = 0
  for (let i = 0; i < end; i++) {
    cp++
    if (isHighSurrogate(text.charCodeAt(i)) && i + 1 < text.length && isLowSurrogate(text.charCodeAt(i + 1))) i++
  }
  return cp
}

/** 码点偏移 → UTF-16 偏移；超出范围时截到文本末尾。 */
export function codePointToUtf16(text: string, cp: number): number {
  let i = 0
  let n = 0
  while (i < text.length && n < cp) {
    if (isHighSurrogate(text.charCodeAt(i)) && i + 1 < text.length && isLowSurrogate(text.charCodeAt(i + 1))) i += 2
    else i++
    n++
  }
  return i
}

/** 文本的码点长度（与 Python len() 一致）。 */
export function codePointLength(text: string): number {
  return utf16ToCodePoint(text, text.length)
}
