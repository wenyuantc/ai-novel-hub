// 角色 / 组织卡片导入导出（ainovel.cards v2 导出，导入兼容 v1；JSON body）

import { api } from './client'
import type { CardExportRequest, CardImportResult, CardPreview, CardResolution, CardResolutions } from './types'

/** 与后端 MAX_CARD_BYTES 一致：按「紧凑序列化后的 UTF-8 字节数」计（导出文件带缩进，文件本身可以略大） */
export const MAX_CARD_BYTES = 2 * 1024 * 1024
/** 读取前按文件大小粗筛的上限（容纳缩进 / 换行），真正的判定见 parseCardText */
export const MAX_CARD_FILE_BYTES = 4 * MAX_CARD_BYTES

export const cardApi = {
  export: (pid: number, body: CardExportRequest) => api.post<Record<string, unknown>>(`/projects/${pid}/cards/export`, body),
  preview: (pid: number, data: unknown) => api.post<CardPreview>(`/projects/${pid}/cards/import/preview`, { data }),
  import: (pid: number, data: unknown, resolutions: CardResolutions) =>
    api.post<CardImportResult>(`/projects/${pid}/cards/import`, { data, resolutions }),
}

const TOO_LARGE = '卡片文件过大，上限为 2MB'

/**
 * 解析卡片文件文本：JSON 语法与大小在本地先查一遍，结构由后端校验。
 * 大小与后端口径一致：紧凑序列化（JSON.stringify，无缩进）后的 UTF-8 字节数。
 */
export function parseCardText(text: string): { data: unknown; error: string | null } {
  if (text.length > MAX_CARD_FILE_BYTES) return { data: null, error: TOO_LARGE }
  let data: unknown
  try {
    data = JSON.parse(text) as unknown
  } catch {
    return { data: null, error: '文件不是有效的 JSON' }
  }
  if (new TextEncoder().encode(JSON.stringify(data)).length > MAX_CARD_BYTES) return { data: null, error: TOO_LARGE }
  return { data, error: null }
}

/** 冲突项的默认处理：全部跳过（与后端「未列出 = 跳过」一致） */
export function defaultResolutions(preview: CardPreview): CardResolutions {
  const pick = (items: CardPreview['characters']) =>
    Object.fromEntries(items.filter((i) => i.status === 'conflict').map((i) => [i.name, 'skip' as CardResolution]))
  return { characters: pick(preview.characters), organizations: pick(preview.organizations) }
}

/** 导入预览统计：新建 / 重命名导入 / 跳过（关联到已有同名对象） */
export function summarizeImport(
  items: CardPreview['characters'],
  choices: Record<string, CardResolution>,
): { created: number; renamed: number; skipped: number } {
  let created = 0
  let renamed = 0
  let skipped = 0
  for (const i of items) {
    if (i.status === 'new') created += 1
    else if (choices[i.name] === 'rename') renamed += 1
    else skipped += 1
  }
  return { created, renamed, skipped }
}

/** 某项最终会以什么名字出现在目标项目中（跳过 = 关联到已有同名对象） */
export function importedName(item: CardPreview['characters'][number], choice: CardResolution | undefined): string {
  if (item.status === 'conflict' && choice === 'rename') return item.rename_to ?? item.name
  return item.name
}

/** 导出文件名：{项目名}-卡片-{YYYYMMDD}.json，去掉文件名非法字符 */
export function cardFilename(projectTitle: string, date = new Date()): string {
  const safe = projectTitle.replace(/[\\/:*?"<>|]/g, '_').trim() || 'ainovel'
  const d = `${date.getFullYear()}${String(date.getMonth() + 1).padStart(2, '0')}${String(date.getDate()).padStart(2, '0')}`
  return `${safe}-卡片-${d}.json`
}

/** 浏览器下载 JSON（object URL + 临时 <a download>） */
export function downloadJson(data: unknown, filename: string): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
