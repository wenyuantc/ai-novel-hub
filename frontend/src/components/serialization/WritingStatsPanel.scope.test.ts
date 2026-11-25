import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, expect, it, vi } from 'vitest'
import type { WritingStatsOverview } from '../../api/writingStats'
import WritingStatsPanel from './WritingStatsPanel'

const view = vi.hoisted(() => ({
  effects: [] as Array<() => void | (() => void)>, writes: vi.fn(),
  overview: vi.fn<(projectId: number, range: unknown, options: { signal: AbortSignal }) => Promise<WritingStatsOverview>>(),
}))
vi.mock('react', async (original) => {
  const react = await original<typeof import('react')>()
  return { ...react, useEffect: (effect: () => void | (() => void)) => { view.effects.push(effect) },
    useState: (initial: unknown) => [react.useState(initial)[0], view.writes] }
})
vi.mock('../../api/writingStats', () => ({ writingStatsApi: { overview: view.overview } }))
vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => ({ canReadNow: () => true }) }))
vi.mock('../../stores/auth', () => ({ useAuthStore: () => 'actor:1' }))

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })
it('切书后即使上一本书的请求迟到，只有新书统计可以回填', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('window', { setTimeout, clearTimeout })
  let resolveOld!: (value: WritingStatsOverview) => void
  const old = new Promise<WritingStatsOverview>((resolve) => { resolveOld = resolve })
  const fresh: WritingStatsOverview = {
    goal: { period: null, target_words: null, revision: 7 }, today: '2026-11-25', days: [],
    tracking_started_at: '2026-11-25T00:00:00Z', start_date: '2026-11-25', end_date: '2026-11-25',
    today_manual_words: 0, today_ai_words: 0, period_manual_words: 0, streak_days: 0,
  }
  view.overview.mockReturnValueOnce(old).mockResolvedValueOnce(fresh)
  const mount = (projectId: number) => {
    view.effects = []
    renderToStaticMarkup(createElement(WritingStatsPanel, { projectId, version: 0 }))
    const cleanups = view.effects.map((effect) => effect())
    return () => cleanups.forEach((cleanup) => cleanup?.())
  }
  const leaveOld = mount(1)
  await vi.advanceTimersByTimeAsync(0)
  leaveOld()
  const leaveNew = mount(2)
  await vi.advanceTimersByTimeAsync(0)
  resolveOld({ ...fresh, goal: { period: 'day', revision: 2, target_words: 90 } })
  await old
  expect(view.overview.mock.calls.map(([id]) => id)).toEqual([1, 2])
  expect(view.overview.mock.calls[0][2].signal.aborted).toBe(true)
  expect(view.writes).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ data: fresh }))
  leaveNew()
})
