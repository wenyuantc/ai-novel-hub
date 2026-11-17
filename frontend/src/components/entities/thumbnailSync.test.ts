import { describe, expect, it, vi } from 'vitest'
import { createThumbnailRevision, settleThumbnailWrite } from './thumbnailSync'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

const original = { version: 'old', mime_type: 'image/webp', width: 100, height: 100 }
const updated = { version: 'new', mime_type: 'image/webp', width: 100, height: 100 }

describe('图片与文字并发', () => {
  it('文字响应迟于多次图片写入时保留最终图片和权限，不影响新文字', async () => {
    const revision = createThumbnailRevision()
    const ticket = revision.capture()
    const delayed = deferred<{ id: number; name: string; thumbnail: typeof original | null; thumbnail_editable: boolean }>()
    let current = { id: 1, name: '旧文字', thumbnail: original as typeof original | null, thumbnail_editable: true }
    const textSave = delayed.promise.then((saved) => { current = revision.merge(saved, current, ticket) })
    revision.changed()
    current = { ...current, thumbnail: updated }
    revision.changed()
    current = { ...current, thumbnail: null, thumbnail_editable: false }
    delayed.resolve({ id: 1, name: '新文字', thumbnail: updated, thumbnail_editable: true })
    await textSave
    expect(current).toEqual({ id: 1, name: '新文字', thumbnail: null, thumbnail_editable: false })
  })

  it('从未发生图片变更或已切换条目时采用文字响应', () => {
    const revision = createThumbnailRevision()
    const ticket = revision.capture()
    const saved = { id: 1, thumbnail: original }
    expect(revision.merge(saved, { id: 1, thumbnail: updated }, ticket)).toEqual(saved)
    revision.changed()
    expect(revision.merge(saved, { id: 2, thumbnail: updated }, ticket)).toEqual(saved)
  })

  it('图片响应晚于弹窗关闭时刷新原作品；切换身份后不刷新也不回填', async () => {
    const delayed = deferred<string>()
    const apply = vi.fn(), invalidate = vi.fn()
    let editorOpen = true, scopeCurrent = true
    const pending = settleThumbnailWrite(() => delayed.promise, () => editorOpen, () => scopeCurrent, apply, invalidate)
    editorOpen = false
    delayed.resolve('new')
    await pending
    expect(apply).not.toHaveBeenCalled()
    expect(invalidate).toHaveBeenCalledOnce()

    const second = deferred<string>()
    const stale = settleThumbnailWrite(() => second.promise, () => editorOpen, () => scopeCurrent, apply, invalidate)
    scopeCurrent = false
    second.resolve('other account')
    await stale
    expect(apply).not.toHaveBeenCalled()
    expect(invalidate).toHaveBeenCalledOnce()
  })

  it('当前编辑器直接接收结果；关闭后结果未知时也刷新原作用域', async () => {
    const apply = vi.fn(), invalidate = vi.fn()
    await settleThumbnailWrite(async () => 'new', () => true, () => true, apply, invalidate)
    expect(apply).toHaveBeenCalledWith('new')
    expect(invalidate).not.toHaveBeenCalled()
    await expect(settleThumbnailWrite(async () => { throw new Error('network') }, () => false, () => true,
      apply, invalidate)).rejects.toThrow('network')
    expect(invalidate).toHaveBeenCalledOnce()
  })
})
