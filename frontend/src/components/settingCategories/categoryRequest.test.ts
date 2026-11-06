import { describe, expect, it, vi } from 'vitest'
import { CategoryRequest } from './categoryRequest'

function deferred<T>() { let resolve!: (value: T) => void; let reject!: (reason: unknown) => void; const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail }); return { promise, resolve, reject } }

describe('分类读取请求', () => {
  it('同版本失败可以重试，后一次请求取代先一次并隔离迟到响应', async () => {
    const old = deferred<string>()
    const load = vi.fn().mockReturnValueOnce(old.promise).mockRejectedValueOnce(new Error('暂时离线')).mockResolvedValueOnce('同版本新结果')
    const controller = new CategoryRequest<string>(load)
    controller.activate()
    const first = controller.refresh()
    await controller.refresh()
    expect(controller.getSnapshot().error).toBe('暂时离线')
    await controller.refresh()
    old.resolve('迟到的旧结果')
    await first
    expect(load).toHaveBeenCalledTimes(3)
    expect(controller.getSnapshot()).toEqual({ data: '同版本新结果', error: null, loading: false })
  })

  it('actor/授权身份改变或撤销读取权限后清除内容且不接收旧结果', async () => {
    const request = deferred<string>()
    let identity = 'actor-a:1'
    let allowed = true
    const controller = new CategoryRequest(() => request.promise, () => allowed, () => identity)
    controller.activate()
    const pending = controller.refresh()
    identity = 'actor-b:1'
    request.resolve('旧账号内容')
    await pending
    expect(controller.getSnapshot().data).toBeNull()
    allowed = false
    controller.invalidate()
    expect(controller.getSnapshot()).toEqual({ data: null, error: null, loading: false })
  })
})
