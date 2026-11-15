import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { isSeriesAccessError } from './seriesAccessContext'
import { useSeriesLoader } from './useSeriesLoader'

const state = vi.hoisted(() => ({
  identity: 'session:1', current: 'session:1',
  authorizationEpoch: 0,
  run: undefined as (() => Promise<unknown>) | undefined,
  accessLoss: vi.fn(),
}))

vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(), useCallback: <T,>(fn: T) => fn,
  useEffect: () => {}, useRef: <T,>(value: T) => ({ current: value }),
}))
vi.mock('../../hooks/useLoader', () => ({
  useLoader: (fn: () => Promise<unknown>) => { state.run = fn; return {} },
}))
vi.mock('./useSeriesAction', () => ({
  useSeriesIdentity: () => state.identity,
  currentSeriesIdentity: () => state.current,
}))
vi.mock('./seriesAccessContext', async (original) => ({
  ...await original<typeof import('./seriesAccessContext')>(),
  useSeriesAccessLoss: () => state.accessLoss,
  useSeriesAuthorization: () => ({ capture: () => state.authorizationEpoch,
    current: (ticket: number) => ticket === state.authorizationEpoch }),
}))

beforeEach(() => {
  state.identity = 'session:1'
  state.current = 'session:1'
  state.run = undefined
  state.authorizationEpoch = 0
  state.accessLoss.mockReset()
})

describe('系列读取的会话与授权隔离', () => {
  it('同一账号重新登录后，迟到的成功响应不会返回给加载器', async () => {
    let resolve!: (value: string) => void
    const pending = new Promise<string>((done) => { resolve = done })
    useSeriesLoader(() => pending)
    const result = state.run!()
    state.current = 'session:2'
    resolve('旧系列内容')
    await expect(result).rejects.toThrow('会话或系列权限已变化')
    expect(state.accessLoss).not.toHaveBeenCalled()
  })

  it('重新邀请建立新授权轮次后，旧系列内容不能落入新界面', async () => {
    let resolve!: (value: string) => void
    const pending = new Promise<string>((done) => { resolve = done })
    useSeriesLoader(() => pending)
    const result = state.run!()
    state.authorizationEpoch += 1
    resolve('旧邀请内容')
    await expect(result).rejects.toThrow('系列权限已变化')
    expect(state.accessLoss).not.toHaveBeenCalled()
  })

  it('读取结果自身发现新授权轮次时，不返回给旧作用域', async () => {
    useSeriesLoader(() => Promise.resolve('新授权系列'), undefined, () => { state.authorizationEpoch += 1 })
    await expect(state.run!()).rejects.toThrow('系列权限已变化')
    expect(state.accessLoss).not.toHaveBeenCalled()
  })

  it('当前会话的 403/404 触发清理；旧会话的失败不得清除新会话', async () => {
    const forbidden = new ApiError(403, '权限变化')
    useSeriesLoader(() => Promise.reject(forbidden))
    await expect(state.run!()).rejects.toBe(forbidden)
    expect(state.accessLoss).toHaveBeenLastCalledWith(forbidden)
    expect(isSeriesAccessError(forbidden)).toBe(true)
    const notFound = new ApiError(404, '权限变化')
    useSeriesLoader(() => Promise.reject(notFound))
    await expect(state.run!()).rejects.toBe(notFound)
    expect(state.accessLoss).toHaveBeenLastCalledWith(notFound)
    expect(isSeriesAccessError(notFound)).toBe(true)
    state.accessLoss.mockClear()
    let reject!: (error: unknown) => void
    useSeriesLoader(() => new Promise((_resolve, fail) => { reject = fail }))
    const result = state.run!()
    state.current = 'session:2'
    reject(new ApiError(404, '旧请求'))
    await expect(result).rejects.toThrow('旧请求')
    expect(state.accessLoss).not.toHaveBeenCalled()
    expect(isSeriesAccessError(new ApiError(409, '版本冲突'))).toBe(false)
    expect(isSeriesAccessError(new ApiError(409, '授权轮次变化', { code: 'stale_series_access' }))).toBe(true)
  })
})
