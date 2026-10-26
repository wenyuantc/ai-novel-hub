// 导入文件的本地预检：JSON 语法与大小（结构由后端校验）

/**
 * 解析导入文件文本。大小口径与后端一致：紧凑序列化（JSON.stringify，无缩进）后的 UTF-8 字节数；
 * 文件本身带缩进可以略大，先按 4 倍上限粗筛文本长度。
 */
export function parseJsonFileText(
  text: string,
  maxBytes: number,
  tooLarge: string,
): { data: unknown; error: string | null } {
  if (text.length > maxBytes * 4) return { data: null, error: tooLarge }
  let data: unknown
  try {
    data = JSON.parse(text) as unknown
  } catch {
    return { data: null, error: '文件不是有效的 JSON' }
  }
  if (new TextEncoder().encode(JSON.stringify(data)).length > maxBytes) return { data: null, error: tooLarge }
  return { data, error: null }
}

/** 导出文件名：{前缀}-{YYYYMMDD}.json */
export function datedFilename(prefix: string, date = new Date()): string {
  const d = `${date.getFullYear()}${String(date.getMonth() + 1).padStart(2, '0')}${String(date.getDate()).padStart(2, '0')}`
  return `${prefix}-${d}.json`
}
