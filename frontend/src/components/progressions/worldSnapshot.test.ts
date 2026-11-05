import { afterEach, describe, expect, it, vi } from 'vitest'
import { readConsistentWorldSnapshot } from './worldSnapshot'

afterEach(() => { vi.unstubAllGlobals() })

const response = (value: unknown) => new Response(JSON.stringify(value), { status: 200 })
const timeline = { items: [{ id: 8 }], total: 1 }
const baseline = (revision: number) => ({
  timeline_id: 8, baseline: { content: '初始剑' }, baseline_status: 'confirmed',
  legacy_compat_allowed: false, timeline_revision: revision,
})
const world = { id: 9, project_id: 4, title: '灵剑', category: 'item', content: '后来的投影', sort: 0 }

describe('世界观编辑快照', () => {
  it('只接受读取条目前后版本一致的基础设定', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(timeline))
      .mockResolvedValueOnce(response(baseline(2)))
      .mockResolvedValueOnce(response(world))
      .mockResolvedValueOnce(response(baseline(2)))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    const result = await readConsistentWorldSnapshot(4, 9, controller.signal)
    expect(result.baseline.baseline.content).toBe('初始剑')
    expect(result.item.content).toBe('后来的投影')
    expect(fetchMock.mock.calls.every(([, options]) => options?.signal === controller.signal)).toBe(true)
  })

  it('读取期间发生并发修改时拒绝混合旧条目和新版本', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(timeline))
      .mockResolvedValueOnce(response(baseline(2)))
      .mockResolvedValueOnce(response(world))
      .mockResolvedValueOnce(response(baseline(3)))
    vi.stubGlobal('fetch', fetchMock)
    await expect(readConsistentWorldSnapshot(4, 9, new AbortController().signal)).rejects.toThrow('设定正在发生变化')
  })

  it('切换条目后即使旧请求迟到，也不会继续读取旧条目', async () => {
    let release: ((response: Response) => void) | undefined
    const fetchMock = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise((resolve) => { release = resolve }))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()
    const pending = readConsistentWorldSnapshot(4, 9, controller.signal)
    controller.abort()
    release?.(response(timeline))
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
