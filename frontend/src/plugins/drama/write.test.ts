import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { User } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { dramaApi } from './api'

const user = { id: 31, created_at: '2026-11-27T20:00:00Z' } as User
const original = useAuthStore.getState()
beforeEach(() => useAuthStore.setState({ user, status: 'authenticated' }))
afterEach(() => {
  useAuthStore.setState(original)
  vi.unstubAllGlobals()
})

describe('短剧写入绑定页面账号', () => {
  it('剧集修改、AI 发起和采纳均带页面身份，保留原版本号及任务键', async () => {
    const fetch = vi.fn(async () => Response.json({}))
    vi.stubGlobal('fetch', fetch)
    await dramaApi.updateEpisode(4, { script: '新剧本', revision: 7 })
    await dramaApi.task(1, 4, 'drama.rewrite', {})
    await dramaApi.apply(1, 4, { id: 12, task_key: 'unique-task-key' } as Parameters<typeof dramaApi.apply>[2])
    const calls = fetch.mock.calls as unknown as [string, RequestInit][]
    for (const [, init] of calls)
      expect(init.headers).toMatchObject({ 'X-Editor-Actor-Id': '31', 'X-Editor-Actor-Created-At': user.created_at })
    expect(calls.map(([, init]) => JSON.parse(String(init.body)))).toEqual([
      { script: '新剧本', revision: 7 },
      { kind: 'drama.rewrite' },
      { task_key: 'unique-task-key' },
    ])
  })
  it('另一标签页已切换账号时，清理失效页面的授权快照', async () => {
    const clear = vi.fn()
    useAuthStore.setState({ clear })
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ detail: '账号已变化', code: 'editor_actor_changed' }, { status: 409 })),
    )
    await expect(dramaApi.deleteEpisode(1, 3)).rejects.toMatchObject({ status: 409 })
    expect(clear).toHaveBeenCalledOnce()
  })
  it('缺少当前用户身份时在本地停止写请求', async () => {
    useAuthStore.setState({ user: null })
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    await expect(dramaApi.deleteEpisode(1, 3)).rejects.toMatchObject({ status: 401 })
    expect(fetch).not.toHaveBeenCalled()
  })
})
