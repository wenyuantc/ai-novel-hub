import { afterEach, describe, expect, it, vi } from 'vitest'
import { deleteThumbnail, MAX_THUMBNAIL_BYTES, putThumbnail, thumbnailFileError, thumbnailUrl, type ThumbnailTarget } from './settingThumbnails'

afterEach(() => vi.unstubAllGlobals())

describe('设定缩略图接口', () => {
  const book: ThumbnailTarget = { scope: 'project', projectId: 3, kind: 'world', entryId: 7 }
  const series: ThumbnailTarget = { scope: 'series', seriesId: 8, entryId: 9 }
  it('作品路径只包含本书实体，版本按查询参数编码', () => {
    expect(thumbnailUrl(book, 'a+b')).toBe('/api/v1/projects/3/settings/world/7/thumbnail?v=a%2Bb')
    expect(thumbnailUrl(series, 'new')).toBe('/api/v1/series/8/entries/9/thumbnail?v=new')
  })
  it('上传 multipart 和删除均携带图片期望版本；系列另携带目录版本', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return new Response(JSON.stringify(init.method === 'DELETE' ? null : { version: 'v2', mime_type: 'image/webp', width: 100, height: 100 }))
    })
    const file = new File(['png'], 'a.png', { type: 'image/png' })
    await putThumbnail(book, file, null, () => true)
    await putThumbnail(series, file, 'v1', () => true, 12)
    await deleteThumbnail(book, 'v2', () => true)
    await deleteThumbnail(series, null, () => true, 13)
    const first = calls[0].init.body as FormData
    const second = calls[1].init.body as FormData
    expect(first.get('file')).toBeInstanceOf(File)
    expect(first.get('expected_version')).toBe('')
    expect(first.has('expected_revision')).toBe(false)
    expect(second.get('expected_version')).toBe('v1')
    expect(second.get('expected_revision')).toBe('12')
    expect(calls[2].url).toBe('/api/v1/projects/3/settings/world/7/thumbnail?expected_version=v2')
    expect(calls[3].url).toBe('/api/v1/series/8/entries/9/thumbnail?expected_version=&expected_revision=13')
    expect(calls.every(({ init }) => init.credentials === 'include')).toBe(true)
    expect((calls[0].init.headers as Record<string, string>)['Content-Type']).toBeUndefined()
  })
  it('限制格式和字节数，不依赖扩展名允许不支持的声明类型', () => {
    expect(thumbnailFileError(new File(['x'], 'ok.JPEG', { type: 'image/jpeg' }))).toBeNull()
    expect(thumbnailFileError(new File(['x'], 'not.png', { type: 'image/svg+xml' }))).toContain('JPG')
    expect(thumbnailFileError(new File(['x'], 'bad.gif', { type: 'image/gif' }))).toContain('JPG')
    expect(thumbnailFileError(new File([new Uint8Array(MAX_THUMBNAIL_BYTES + 1)], 'big.webp', { type: 'image/webp' }))).toContain('10 MiB')
  })
})
