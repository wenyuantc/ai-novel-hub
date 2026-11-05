import { describe, expect, it, vi } from 'vitest'
import type { ChapterSettingContext } from '../../api/types'
import { SettingContextPreviewController } from './settingContextPreviewController'

function context(chapterId: number, text: string): ChapterSettingContext {
  return {
    position: { chapter_id: chapterId, chapter_no: 1, boundary: 'start' },
    text,
    legacy_unverified_count: 0,
    warnings: [],
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail })
  return { promise, resolve, reject }
}

function setup() {
  const fetch = vi.fn<(projectId: number, chapterId: number, signal: AbortSignal) => Promise<ChapterSettingContext>>()
    .mockResolvedValue(context(7, '当前设定'))
  let allowed = true
  const controller = new SettingContextPreviewController(3, 7, fetch, () => allowed)
  return { fetch, controller, setAllowed: (value: boolean) => { allowed = value } }
}

describe('章节设定预览请求', () => {
  it('只在激活且有读取权限时请求，并展示当前章节的响应', async () => {
    const { fetch, controller, setAllowed } = setup()
    await controller.load()
    controller.activate()
    setAllowed(false)
    await controller.load()
    expect(fetch).not.toHaveBeenCalled()

    const result = deferred<ChapterSettingContext>()
    fetch.mockReturnValueOnce(result.promise)
    setAllowed(true)
    const loading = controller.load()
    expect(controller.getSnapshot()).toMatchObject({ loading: true, data: null, error: null })
    expect(fetch).toHaveBeenCalledExactlyOnceWith(3, 7, expect.any(AbortSignal))
    result.resolve(context(7, '章初设定'))
    await loading
    expect(controller.getSnapshot()).toMatchObject({ loading: false, data: { text: '章初设定' }, error: null })
  })

  it('刷新会取消旧请求，忽略无视取消而晚到的响应', async () => {
    const { fetch, controller } = setup()
    const old = deferred<ChapterSettingContext>()
    const latest = deferred<ChapterSettingContext>()
    fetch.mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise)
    controller.activate()
    const first = controller.load()
    const oldSignal = fetch.mock.calls[0][2]
    const second = controller.load()
    expect(oldSignal.aborted).toBe(true)
    latest.resolve(context(7, '新设定'))
    await second
    old.resolve(context(7, '旧设定'))
    await first
    expect(controller.getSnapshot()).toMatchObject({ loading: false, data: { text: '新设定' }, error: null })
  })

  it('折叠或离开章节时取消并清空数据，重新激活后旧响应不能复活', async () => {
    const { fetch, controller } = setup()
    await controller.load()
    controller.activate()
    await controller.load()
    const old = deferred<ChapterSettingContext>()
    fetch.mockReturnValueOnce(old.promise)
    const first = controller.load()
    const oldSignal = fetch.mock.calls[1][2]
    controller.deactivate()
    expect(oldSignal.aborted).toBe(true)
    expect(controller.getSnapshot()).toEqual({ loading: false, data: null, error: null })
    controller.activate()
    await controller.load()
    old.resolve(context(7, '已关闭的响应'))
    await first
    expect(controller.getSnapshot()).toMatchObject({ loading: false, data: { text: '当前设定' }, error: null })
  })

  it('权限在响应前撤销时不保留或发布任何预览，也不会继续请求', async () => {
    const { fetch, controller, setAllowed } = setup()
    controller.activate()
    await controller.load()
    const result = deferred<ChapterSettingContext>()
    fetch.mockReturnValueOnce(result.promise)
    const loading = controller.load()
    setAllowed(false)
    result.resolve(context(7, '已无权读取'))
    await loading
    expect(controller.getSnapshot()).toEqual({ loading: false, data: null, error: null })
    await controller.load()
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('切换项目、章节和身份时，新范围不复用旧数据，旧响应无法影响新范围', async () => {
    const oldResult = deferred<ChapterSettingContext>()
    const oldFetch = vi.fn().mockReturnValue(oldResult.promise)
    let identity = '旧身份'
    const oldController = new SettingContextPreviewController(3, 7, oldFetch, () => identity === '旧身份')
    oldController.activate()
    const oldLoad = oldController.load()

    identity = '新身份'
    const newFetch = vi.fn().mockResolvedValue(context(9, '新章节设定'))
    const newController = new SettingContextPreviewController(4, 9, newFetch, () => identity === '新身份')
    expect(newController.getSnapshot()).toEqual({ loading: false, data: null, error: null })
    newController.activate()
    await newController.load()
    oldResult.resolve(context(7, '旧章节设定'))
    await oldLoad

    expect(oldController.getSnapshot()).toEqual({ loading: false, data: null, error: null })
    expect(newFetch).toHaveBeenCalledExactlyOnceWith(4, 9, expect.any(AbortSignal))
    expect(newController.getSnapshot().data?.text).toBe('新章节设定')
  })

  it('错误可重试；服务端返回其他章节时不显示错误的数据', async () => {
    const { fetch, controller } = setup()
    controller.activate()
    fetch.mockRejectedValueOnce(new Error('暂时无法加载'))
    await controller.load()
    expect(controller.getSnapshot()).toEqual({ loading: false, data: null, error: '暂时无法加载' })

    fetch.mockResolvedValueOnce(context(8, '其他章节的设定'))
    await controller.load()
    expect(controller.getSnapshot()).toMatchObject({ loading: false, data: null, error: expect.any(String) })
    await controller.load()
    expect(controller.getSnapshot()).toEqual({ loading: false, data: context(7, '当前设定'), error: null })
  })
})
