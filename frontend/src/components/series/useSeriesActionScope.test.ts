import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { useSeriesAction } from './useSeriesAction'

const state = vi.hoisted(() => ({
  generation: 1,
  authorizationEpoch: 0,
  auth: { status: 'authenticated', user: null },
  accessLoss: vi.fn(),
  cleanup: undefined as (() => void) | undefined,
}))

vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(),
  useLayoutEffect: (effect: () => void | (() => void)) => { state.cleanup = effect() || undefined },
  useRef: <T,>(value: T) => ({ current: value }),
  useState: <T,>(value: T) => [value, vi.fn()],
  useSyncExternalStore: (_subscribe: unknown, current: () => number) => current(),
}))
vi.mock('../../api/client', async (original) => ({
  ...await original<typeof import('../../api/client')>(),
  currentRequestGeneration: () => state.generation,
  watchRequestGeneration: () => ({ generation: state.generation, dispose: vi.fn() }),
}))
vi.mock('../../stores/auth', () => ({
  useAuthStore: Object.assign((selector: (auth: typeof state.auth) => unknown) => selector(state.auth), {
    getState: () => state.auth,
  }),
}))
vi.mock('./seriesAccessContext', () => ({
  useSeriesAccessLoss: () => state.accessLoss,
  useSeriesAuthorization: () => ({ capture: () => state.authorizationEpoch,
    current: (ticket: number) => ticket === state.authorizationEpoch }),
}))

beforeEach(() => {
  state.generation = 1
  state.authorizationEpoch = 0
  state.accessLoss.mockReset()
  state.cleanup = undefined
})

describe('系列写请求的会话作用域', () => {
  it('同一账号重新登录后不接收旧写入成功响应', async () => {
    let resolve!: (value: string) => void
    const pending = new Promise<string>((done) => { resolve = done })
    const success = vi.fn()
    const action = useSeriesAction()
    const work = action.run(() => pending, success)
    state.generation += 1
    resolve('旧结果')
    await work
    expect(success).not.toHaveBeenCalled()
    expect(state.accessLoss).not.toHaveBeenCalled()
  })

  it('卸载后丢弃旧结果，当前会话的 404 通知清除内容', async () => {
    let resolve!: (value: string) => void
    const pending = new Promise<string>((done) => { resolve = done })
    const success = vi.fn()
    const action = useSeriesAction()
    const work = action.run(() => pending, success)
    state.cleanup?.()
    resolve('旧结果')
    await work
    expect(success).not.toHaveBeenCalled()

    const error = new ApiError(404, '系列不存在')
    const current = useSeriesAction()
    await current.run(() => Promise.reject(error), success)
    expect(state.accessLoss).toHaveBeenCalledWith(error)
    expect(success).not.toHaveBeenCalled()
  })

  it('系列重新授权之后，旧轮次的写入响应不能改变新界面', async () => {
    let resolve!: (value: string) => void
    const pending = new Promise<string>((done) => { resolve = done })
    const success = vi.fn()
    const action = useSeriesAction()
    const work = action.run(() => pending, success)
    state.authorizationEpoch += 1
    resolve('旧轮次结果')
    await work
    expect(success).not.toHaveBeenCalled()
  })
})
