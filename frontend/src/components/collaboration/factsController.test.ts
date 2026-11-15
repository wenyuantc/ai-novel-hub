import { describe, expect, it, vi } from 'vitest'
import { ApiError, invalidateRequestGeneration } from '../../api/client'
import { FactsController, type FactsState } from './factsController'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

describe('协作事实控制器', () => {
  it('409 后只重新读取邀请，不重放旧轮次并保留最新版本', async () => {
    const revised = deferred<string[]>()
    const read = vi.fn().mockResolvedValueOnce(['旧邀请#1']).mockReturnValueOnce(revised.promise)
    const writes = vi.fn().mockRejectedValue(new ApiError(409, '邀请已变化'))
    const states: FactsState<string[]>[] = []
    const settled = vi.fn()
    const controller = new FactsController<string[]>(read, () => true, (state) => states.push(state), settled)
    await controller.refresh()
    const pending = controller.mutate(writes, '已接受')
    await Promise.resolve()
    expect(writes).toHaveBeenCalledTimes(1)
    expect(read).toHaveBeenCalledTimes(2)
    expect(states.at(-1)).toMatchObject({ busy: true, stale: true, data: ['旧邀请#1'] })
    revised.resolve(['新邀请#2'])
    expect(await pending).toBe(false)
    expect(states.at(-1)).toMatchObject({ data: ['新邀请#2'], busy: false, stale: false })
    expect(states.at(-1)?.notice).toContain('状态已变化')
    expect(writes).toHaveBeenCalledTimes(1)
    expect(settled).toHaveBeenCalledOnce()
    controller.dispose()
  })

  it('结果未知且刷新失败时保留已显示事实但禁止写入，刷新成功才重新允许', async () => {
    const read = vi.fn().mockResolvedValueOnce(['旧成员']).mockRejectedValueOnce(new Error('断网'))
      .mockResolvedValueOnce(['已变更成员'])
    const action = vi.fn().mockRejectedValue(new ApiError(0, '网络错误'))
    const states: FactsState<string[]>[] = []
    const controller = new FactsController<string[]>(read, () => true, (state) => states.push(state))
    await controller.refresh()
    await controller.mutate(action, '已更新')
    expect(states.at(-1)).toMatchObject({ data: ['旧成员'], busy: false, stale: true })
    expect(states.at(-1)?.notice).toContain('结果尚未确认')
    expect(await controller.mutate(action, '已更新')).toBe(false)
    expect(action).toHaveBeenCalledOnce()
    await controller.refresh()
    expect(states.at(-1)).toMatchObject({ data: ['已变更成员'], stale: false })
    controller.dispose()
  })

  it('新请求、会话代次、换用户及卸载均使旧响应失效', async () => {
    const first = deferred<string[]>()
    const second = deferred<string[]>()
    const read = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    let actor = 1
    const states: FactsState<string[]>[] = []
    const controller = new FactsController<string[]>(read, () => actor === 1, (state) => states.push(state))
    const old = controller.refresh()
    const latest = controller.refresh()
    second.resolve(['新事实'])
    await latest
    first.resolve(['旧事实'])
    await old
    expect(states.at(-1)?.data).toEqual(['新事实'])
    const pending = deferred<unknown>()
    const mutation = controller.mutate(() => pending.promise, '成功')
    actor = 2
    invalidateRequestGeneration()
    pending.resolve({})
    expect(await mutation).toBe(false)
    expect(read).toHaveBeenCalledTimes(2)
    controller.dispose()
    expect(states.at(-1)?.data).toEqual(['新事实'])
  })

  it('明确失权时清空旧成员，网络故障不误当作空列表', async () => {
    const read = vi.fn().mockResolvedValueOnce(['成员']).mockRejectedValueOnce(new ApiError(403, '无权访问'))
      .mockResolvedValueOnce(['重新授权'])
    const states: FactsState<string[]>[] = []
    const controller = new FactsController<string[]>(read, () => true, (state) => states.push(state))
    await controller.refresh()
    await controller.refresh()
    expect(states.at(-1)).toMatchObject({ data: null, stale: true, accessEpoch: 1 })
    expect(states.at(-1)?.error).toContain('无权访问')
    await controller.refresh()
    expect(states.at(-1)).toMatchObject({ data: ['重新授权'], stale: false, accessEpoch: 1 })
    controller.dispose()
  })

  it('写请求在卸载后返回时不刷新、不落地、不通知父页面', async () => {
    const read = vi.fn().mockResolvedValue(['成员'])
    const result = deferred<unknown>()
    const states: FactsState<string[]>[] = []
    const settled = vi.fn()
    const controller = new FactsController<string[]>(read, () => true, (state) => states.push(state), settled)
    await controller.refresh()
    const pending = controller.mutate(() => result.promise, '已更新')
    const count = states.length
    controller.dispose()
    result.resolve({})
    expect(await pending).toBe(false)
    expect(states).toHaveLength(count)
    expect(read).toHaveBeenCalledTimes(1)
    expect(settled).not.toHaveBeenCalled()
  })

  it('确认只发一次请求；actor 改变后旧控制器不能发起写入', async () => {
    const read = vi.fn().mockResolvedValue(['待处理邀请'])
    const response = deferred<unknown>()
    const action = vi.fn().mockReturnValue(response.promise)
    let actor = 1
    const controller = new FactsController<string[]>(read, () => actor === 1, () => {})
    await controller.refresh()
    const pending = controller.mutate(action, '已接受')
    expect(await controller.mutate(action, '已接受')).toBe(false)
    expect(action).toHaveBeenCalledOnce()
    response.resolve({})
    expect(await pending).toBe(true)
    actor = 2
    expect(await controller.mutate(action, '已接受')).toBe(false)
    expect(action).toHaveBeenCalledOnce()
    controller.dispose()
  })
})
