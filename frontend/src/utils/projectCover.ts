import type { CoverPreview } from '../api/types'

export const COVER_MAX_BYTES = 10 * 1024 * 1024
export const COVER_ACCEPT = 'image/jpeg,image/png,image/webp'

/** 前端先检查类型与大小，后端仍按实际文件内容最终校验。 */
export function precheckCoverUpload(file: { type: string; size: number }): string | null {
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) return '仅支持 JPG、PNG、WebP 图片'
  if (file.size === 0) return '封面文件为空'
  if (file.size > COVER_MAX_BYTES) return '封面不能超过 10MB'
  return null
}

/** 确认预览时保留原始图片字节，经同一个上传接口设置封面。 */
export function coverPreviewBlob(preview: CoverPreview): Blob {
  const raw = atob(preview.image_base64)
  const bytes = Uint8Array.from(raw, (c) => c.charCodeAt(0))
  return new Blob([bytes], { type: preview.mime_type })
}
