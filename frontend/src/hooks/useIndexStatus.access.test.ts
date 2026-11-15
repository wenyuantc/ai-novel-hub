import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PollingVisibility } from '../utils/visiblePolling'
import type { IndexStatus } from '../api/types'
import { currentIndexStatus, INDEX_POLL_MS, indexStatusKey, startIndexStatusPolling } from './useIndexStatus'

// E6-e：协作者的 kb.index 实时订阅为 404，建索引进度改为只在页面可见时轮询；作者保持原定时刷新。
function fakeVisibility(initial: DocumentVisibilityState = 'visible') {
  const listeners = new Set<() => void>()
  const target: PollingVisibility & { set: (state: DocumentVisibilityState) => void; size: () => number } = {
    visibilityState: initial,
    addEventListener: (_type, listener) => { listeners.add(listener) },
    removeEventListener: (_type, listener) => { listeners.delete(listener) },
    set(state) { (this as { visibilityState: DocumentVisibilityState }).visibilityState = state; for (const l of [...listeners]) l() },
    size: () => listeners.size,
  }
  return target
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('索引进度轮询节奏', () => {
  it('协作者：可见时按间隔刷新，隐藏后暂停，回到前台立即刷新；清理后完全停止', () => {
    const tick = vi.fn()
    const visibility = fakeVisibility()
    const stop = startIndexStatusPolling(tick, true, visibility)
    vi.advanceTimersByTime(INDEX_POLL_MS * 2)
    expect(tick).toHaveBeenCalledTimes(2)
    visibility.set('hidden')
    vi.advanceTimersByTime(INDEX_POLL_MS * 3)
    expect(tick).toHaveBeenCalledTimes(2)
    visibility.set('visible')
    expect(tick).toHaveBeenCalledTimes(3)
    stop?.()
    expect(visibility.size()).toBe(0)
    vi.advanceTimersByTime(INDEX_POLL_MS * 3)
    expect(tick).toHaveBeenCalledTimes(3)
  })

  it('协作者在后台打开页面时不轮询；没有 visibility（非浏览器）时不轮询', () => {
    const tick = vi.fn()
    const stop = startIndexStatusPolling(tick, true, fakeVisibility('hidden'))
    vi.advanceTimersByTime(INDEX_POLL_MS * 3)
    expect(tick).not.toHaveBeenCalled()
    stop?.()
    expect(startIndexStatusPolling(tick, true, undefined)).toBeUndefined()
  })

  it('作者保持原定时刷新，不受页面可见性影响', () => {
    const tick = vi.fn()
    const visibility = fakeVisibility('hidden')
    const stop = startIndexStatusPolling(tick, false, visibility)
    vi.advanceTimersByTime(INDEX_POLL_MS * 2)
    expect(tick).toHaveBeenCalledTimes(2)
    expect(visibility.size()).toBe(0)
    stop?.()
    vi.advanceTimersByTime(INDEX_POLL_MS * 2)
    expect(tick).toHaveBeenCalledTimes(2)
  })
})

describe('索引状态的读取键', () => {
  const vector: IndexStatus = { embedding_model: null, needs_rebuild: false, stale_count: 0, stale: { kb: 0, memory: 0 },
    indexing: false, owner_embedding_configured: true, retrieval_mode: 'vector', keyword_reason: null }

  it('授权轮次 / 模型授权变化后，新状态读到前不沿用旧的「向量可用」结果', () => {
    const before = indexStatusKey(2, 'grant-1:1')
    const loaded = { key: before, status: vector }
    expect(currentIndexStatus(loaded, before, true)).toBe(vector)
    // 作者关闭模型授权（allow_owner_ai 1 → 0）或授权轮次变化
    expect(currentIndexStatus(loaded, indexStatusKey(2, 'grant-1:0'), true)).toBeUndefined()
    expect(currentIndexStatus(loaded, indexStatusKey(2, 'grant-2:1'), true)).toBeUndefined()
  })

  it('切换作品或关闭读取（降为查看者）时不返回旧状态', () => {
    const loaded = { key: indexStatusKey(2, 'k'), status: vector }
    expect(currentIndexStatus(loaded, indexStatusKey(3, 'k'), true)).toBeUndefined()
    expect(currentIndexStatus(loaded, indexStatusKey(2, 'k'), false)).toBeUndefined()
    expect(currentIndexStatus(undefined, indexStatusKey(2, 'k'), true)).toBeUndefined()
  })
})
