import { buildUrl, request } from './client'
import type { SettingThumbnailInfo } from './types'

export type ThumbnailKind = 'character' | 'world' | 'organization' | 'career'
export type ThumbnailTarget =
  | { scope: 'project'; projectId: number; kind: ThumbnailKind; entryId: number }
  | { scope: 'series'; seriesId: number; entryId: number }

export const thumbnailPath = (target: ThumbnailTarget) => target.scope === 'project'
  ? `/projects/${target.projectId}/settings/${target.kind}/${target.entryId}/thumbnail`
  : `/series/${target.seriesId}/entries/${target.entryId}/thumbnail`

export function thumbnailUrl(target: ThumbnailTarget, version: string): string {
  return buildUrl(thumbnailPath(target), { v: version })
}

export const MAX_THUMBNAIL_BYTES = 10 * 1024 * 1024
export function thumbnailFileError(file: File): string | null {
  if (file.size > MAX_THUMBNAIL_BYTES) return '图片不能超过 10 MiB'
  if (file.type ? !['image/jpeg', 'image/png', 'image/webp'].includes(file.type) : !/\.(jpe?g|png|webp)$/i.test(file.name)) {
    return '请选择 JPG、PNG 或 WebP 图片'
  }
  return null
}

export function putThumbnail(target: ThumbnailTarget, file: File, version: string | null, current: () => boolean, revision?: number) {
  const body = new FormData()
  body.append('file', file)
  body.append('expected_version', version ?? '')
  if (revision !== undefined) body.append('expected_revision', String(revision))
  return request<SettingThumbnailInfo | { thumbnail: SettingThumbnailInfo | null; revision: number }>(thumbnailPath(target), {
    method: 'PUT', body, isCurrent: current,
  })
}

export function deleteThumbnail(target: ThumbnailTarget, version: string | null, current: () => boolean, revision?: number) {
  return request<null | { thumbnail: SettingThumbnailInfo | null; revision: number }>(thumbnailPath(target), {
    method: 'DELETE', query: { expected_version: version ?? '', expected_revision: revision }, isCurrent: current,
  })
}
