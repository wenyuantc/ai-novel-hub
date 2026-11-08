import type { RichDocument, RichFormatting } from '../../api/richDocument'

// 与服务端 SceneDocument / TextFormatting 的结构上限一致；超限只在保存时提示，不截断草稿。
const MAX_SECTIONS = 1_000
const MAX_SECTION_CHARS = 500_000
const MAX_SECTION_TOTAL_CHARS = 2_000_000
const MAX_METADATA_ITEMS = 20_000

function codePointLength(text: string): number {
  let count = 0
  for (let index = 0; index < text.length; count++) {
    const code = text.codePointAt(index)!
    index += code > 0xffff ? 2 : 1
  }
  return count
}

function formattingLimit(label: string, value: RichFormatting): string | null {
  const items = [
    ['段落边界', value.boundaries], ['文字样式', value.inline], ['段落样式', value.blocks],
  ] as const
  const excess = items.find(([, list]) => list.length > MAX_METADATA_ITEMS)
  return excess ? `${label}的${excess[0]}超过 2 万项上限，请减少后保存；草稿仍保留。` : null
}

/** 仅预检可以精确计算的项目；8 MiB 元数据字节数由服务端最终校验。 */
export function richDocumentCapacityProblem(document: RichDocument): string | null {
  if (document.sections.length > MAX_SECTIONS) return '当前场景的区块超过 1000 个上限，请减少后保存；草稿仍保留。'
  if (document.retired_section_keys.length > MAX_METADATA_ITEMS) return '当前场景的退场区块身份超过 2 万个上限；草稿仍保留。'
  if (document.markers.length > MAX_METADATA_ITEMS) return '当前场景的标记超过 2 万条上限，请减少后保存；草稿仍保留。'
  const bodyProblem = formattingLimit('正文', document.formatting)
  if (bodyProblem) return bodyProblem
  let total = 0
  for (const [index, section] of document.sections.entries()) {
    const length = codePointLength(section.content)
    if (length > MAX_SECTION_CHARS) return `第 ${index + 1} 个区块的正文超过 50 万字符上限，请减少后保存；草稿仍保留。`
    total += length
    const problem = formattingLimit(`第 ${index + 1} 个区块`, section.formatting)
    if (problem) return problem
  }
  if (total > MAX_SECTION_TOTAL_CHARS) return '当前场景的区块正文合计超过 200 万字符上限，请减少后保存；草稿仍保留。'
  return null
}

/** 服务端按默认字段、身份与 UTF-8 实际编码校验，不能用浏览器估算误拒合法稿。 */
export function richDocumentSaveError(detail: string): string {
  if (detail.includes('文档元数据总量超过上限') || detail.includes('富文档元数据总量超过上限')) {
    return '正文格式、区块和标记的元数据合计超过 8 MiB 上限，请减少格式或标记后保存；草稿仍保留。'
  }
  return detail
}
