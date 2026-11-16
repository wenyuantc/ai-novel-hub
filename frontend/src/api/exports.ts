import { rawRequest } from './client'
import { decodeExportWarnings, exportQuery, parseContentDispositionFilename, type ExportOptions } from './projects'
import type { SettingEntryRef } from './types'

export type DocumentFormat = 'txt' | 'md' | 'docx'
export type StoryScope = 'all' | 'selected' | 'none'
export type StoryUnit = 'chapter' | 'scene'
export type StoryContent = 'prose' | 'summary' | 'both'

/** 与站内内容组合导出 schema 一致；空 id 集不表示全部。 */
export interface DocumentExportRequest {
  format: DocumentFormat
  story: { scope: StoryScope; unit: StoryUnit; content: StoryContent; ids: number[] }
  materials: { settings: SettingEntryRef[]; conversation_ids: number[]; note_ids: number[] }
}

interface DownloadOptions {
  signal?: AbortSignal
  /** 捕获的弹窗和作品权限作用域；blob 就绪后再次核验。 */
  isCurrent: () => boolean
  save?: (blob: Blob, filename: string) => void
}

export interface ExportDownloadResult { filename: string; warnings: string[] }

function saveAttachment(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  try {
    anchor.href = url
    anchor.download = filename
    document.body.appendChild(anchor)
    anchor.click()
  } finally {
    anchor.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
}

async function receiveAttachment(response: Response, fallback: string, options: DownloadOptions): Promise<ExportDownloadResult | null> {
  const blob = await response.blob()
  if (options.signal?.aborted || !options.isCurrent()) return null
  const filename = parseContentDispositionFilename(response.headers.get('content-disposition')) ?? fallback
  ;(options.save ?? saveAttachment)(blob, filename)
  return { filename, warnings: decodeExportWarnings(response.headers.get('x-export-warnings')) }
}

/** POST 下载只在请求和 blob 读取完成后仍处于原授权作用域时触发。 */
export async function downloadDocument(
  projectId: number, body: DocumentExportRequest, options: DownloadOptions,
): Promise<ExportDownloadResult | null> {
  if (options.signal?.aborted || !options.isCurrent()) return null
  const response = await rawRequest(`/projects/${projectId}/export-document`, {
    method: 'POST', body, signal: options.signal, isCurrent: options.isCurrent,
  })
  return receiveAttachment(response, `novel.${body.format}`, options)
}

/** EPUB 沿用原 GET 请求与旧的空选含义，同时保护晚到下载。 */
export async function downloadEpub(
  projectId: number, options: ExportOptions, guard: DownloadOptions,
): Promise<ExportDownloadResult | null> {
  if (guard.signal?.aborted || !guard.isCurrent()) return null
  const response = await rawRequest(`/projects/${projectId}/export`, {
    query: exportQuery('epub', options), signal: guard.signal, isCurrent: guard.isCurrent,
  })
  return receiveAttachment(response, 'novel.epub', guard)
}
