import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { WritingStatsOverview } from '../../api/writingStats'
import WritingStatsPanel from './WritingStatsPanel'
import WritingGoalModal from './WritingGoalModal'

const view = vi.hoisted(() => ({
  injectRange: false, rangeError: '', data: undefined as WritingStatsOverview | undefined, error: '', canEdit: true, loading: false,
  onSave: () => {}, reload: vi.fn(), setGoal: vi.fn(), overview: vi.fn(), setState: vi.fn(),
}))
vi.mock('react', async (original) => {
  const react = await original<typeof import('react')>()
  return { ...react, useState: (initial: unknown) => {
    const inject = view.injectRange && initial === undefined
    if (inject) view.injectRange = false
    const [value, setValue] = react.useState(inject ? { start_date: '2026-11-22', end_date: '2026-11-22' } : initial)
    return [value, (next: unknown) => { view.setState(next); setValue(next) }]
  } }
})
vi.mock('../../hooks/useMentionQuery', () => ({ useMentionQuery: (_projectId: number, key: string) => ({
  data: key.startsWith('writing-trend:') ? undefined : view.data,
  error: key.startsWith('writing-trend:') ? view.rangeError : view.error, loading: view.loading, reload: view.reload,
}) }))
vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => ({ canReadNow: () => true,
  canEdit: view.canEdit, canEditNow: () => view.canEdit }) }))
vi.mock('../../api/writingStats', () => ({ writingStatsApi: { setGoal: view.setGoal, overview: view.overview } }))
vi.mock('antd', async (original) => ({
  ...await original<typeof import('antd')>(),
  Modal: ({ onOk }: { onOk: () => void }) => { view.onSave = onOk; return null },
}))
vi.mock('./useSerializationAction', () => ({ useSerializationAction: () => ({
  run: async (request: (opts: unknown) => Promise<unknown>, success: (result: unknown) => void) =>
    success(await request({ isCurrent: () => true })),
}) }))

const sample: WritingStatsOverview = {
  tracking_started_at: '2026-11-23T01:02:00Z', today: '2026-11-25', start_date: '2026-11-22', end_date: '2026-11-24',
  today_manual_words: 2000, today_ai_words: 5000, period_manual_words: 3100, streak_days: 3,
  goal: { period: 'week', target_words: 3000, revision: 1 },
  days: [
    { date: '2026-11-22', manual_words: null, ai_words: null, partial: false },
    { date: '2026-11-23', manual_words: 0, ai_words: 0, partial: true },
    { date: '2026-11-24', manual_words: 1100, ai_words: 900, partial: false },
  ],
}
const render = () => renderToStaticMarkup(createElement(WritingStatsPanel, { projectId: 5, version: 1 }))
beforeEach(() => {
  vi.clearAllMocks()
  Object.assign(view, { injectRange: false, rangeError: '', data: sample, loading: false, error: '', canEdit: true })
})
describe('个人写作量工作台', () => {
  it('筛选到昨天仍展示今日两类写作量、完整周期进度和达标状态', () => {
    const html = render()
    for (const text of ['2,000', '5,000', '本周手写 3,100 / 3,000 字', '已达标', 'max="3000" value="3000"',
      '1,100', '900', '部分日', '尚未开始统计', '2026-11-23 09:02']) expect(html).toContain(text)
    expect(html).toContain('修改码字目标')
  })
  it('自选日期加载失败仍保留本期进度、设置入口与选定日期', () => {
    Object.assign(view, { injectRange: true, rangeError: '所选日期暂时不可用' })
    const html = render()
    for (const text of ['本周手写 3,100 / 3,000 字', '修改码字目标', '所选日期暂时不可用',
      'value="2026-11-22"', '查看趋势']) expect(html).toContain(text)
    expect(html).not.toContain('每日写作量明细')
  })
  it('未设目标时展示写作量，只读参与者看不到编辑入口', () => {
    view.data = { ...sample, goal: { period: null, target_words: null, revision: 2 } }
    expect(render()).toContain('设置码字目标</span>')
    view.canEdit = false
    const html = render()
    expect(html).toContain('尚未设置个人码字目标')
    expect(html).toContain('2,000')
    expect(html).not.toContain('设置码字目标</span>')
  })
  it('首次加载显示状态，读取失败提供重试入口', () => {
    Object.assign(view, { data: undefined, loading: true })
    expect(render()).toContain('正在加载')
    Object.assign(view, { error: '读取统计失败', loading: false })
    expect(render()).toContain('读取统计失败')
    expect(render()).toMatch(/重\s*试/)
  })
  it('保存遇到版本冲突时读取当前目标，不自动覆盖，也不关闭设置', async () => {
    const fresh = { period: null, target_words: null, revision: 2 }
    view.setGoal.mockRejectedValueOnce(new ApiError(409, '版本冲突'))
    view.overview.mockResolvedValueOnce({ ...sample, goal: fresh })
    const onSaved = vi.fn()
    renderToStaticMarkup(createElement(WritingGoalModal, { projectId: 5, initial: sample.goal, onClose: vi.fn(), onSaved }))
    view.onSave()
    await vi.waitFor(() => expect(view.setState).toHaveBeenCalledWith(fresh))
    expect(view.setGoal).toHaveBeenCalledExactlyOnceWith(5, { period: 'week', target_words: 3000, expected_revision: 1 }, expect.anything())
    expect(onSaved).not.toHaveBeenCalled()
  })
})
