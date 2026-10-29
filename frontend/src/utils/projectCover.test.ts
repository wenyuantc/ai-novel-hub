import { describe, expect, it } from 'vitest'
import { COVER_MAX_BYTES, coverPreviewBlob, precheckCoverUpload } from './projectCover'

describe('封面上传预检', () => {
  it('允许 JPG、PNG、WebP，恰好 10MB 也可上传', () => {
    for (const type of ['image/jpeg', 'image/png', 'image/webp']) {
      expect(precheckCoverUpload({ type, size: COVER_MAX_BYTES })).toBeNull()
    }
  })

  it('拒绝空文件、超限图片和不支持的类型', () => {
    expect(precheckCoverUpload({ type: 'image/png', size: 0 })).toBe('封面文件为空')
    expect(precheckCoverUpload({ type: 'image/png', size: COVER_MAX_BYTES + 1 })).toBe('封面不能超过 10MB')
    expect(precheckCoverUpload({ type: 'image/gif', size: 100 })).toBe('仅支持 JPG、PNG、WebP 图片')
    expect(precheckCoverUpload({ type: 'text/plain', size: 100 })).toBe('仅支持 JPG、PNG、WebP 图片')
  })
})

describe('生成预览转上传图片', () => {
  it('保留图片字节与 MIME 类型', async () => {
    const blob = coverPreviewBlob({ image_base64: 'AAH+/w==', mime_type: 'image/webp' })
    expect(blob.type).toBe('image/webp')
    expect(Array.from(new Uint8Array(await blob.arrayBuffer()))).toEqual([0, 1, 254, 255])
  })
})
