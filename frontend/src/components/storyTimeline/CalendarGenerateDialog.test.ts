import { createElement, type EffectCallback, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { storyTimelineApi, type GeneratedCalendar } from '../../api/storyTimeline'
import { deferred } from '../systemSettings/systemSettingsTestFixtures'
import CalendarDialog from './CalendarDialog'
import CalendarGenerateDialog from './CalendarGenerateDialog'

const view = vi.hoisted(() => ({
  source: 'own', allowed: true, effects: [] as EffectCallback[], submit: () => Promise.resolve() as Promise<void> | undefined,
}))
vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(),
  useLayoutEffect: (effect: EffectCallback) => { view.effects.push(effect) },
}))
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  const Form = Object.assign(({ children, onFinish }: { children: ReactNode; onFinish: typeof view.submit }) => {
    view.submit = onFinish
    return createElement('form', null, children)
  }, { Item: ({ children }: { children: ReactNode }) => children })
  return { ...actual, Form, Modal: ({ children }: { children: ReactNode }) => children }
})
vi.mock('../AIConfigSelect', () => ({ default: () => null }))
vi.mock('../../hooks/useAIModelSource', () => ({ useAIModelSource: () => view.source }))
vi.mock('../../hooks/useProjectAccess', () => ({
  useProjectAccess: () => ({ canEditNow: () => true, canUseAI: view.allowed, canUseAINow: () => view.allowed }),
}))

const calendar: GeneratedCalendar = { revision: 2, era_name: '潮历', months: [{ id: 'tide', name: '春汛', days: 30 }] }
function renderGenerator() {
  const onAdopt = vi.fn()
  const onBaseChanged = vi.fn()
  const html = renderToStaticMarkup(createElement(CalendarGenerateDialog, {
    pid: 5, revision: 2, onClose: vi.fn(), onBaseChanged, onAdopt,
  }))
  return { onAdopt, onBaseChanged, html }
}
afterEach(() => { vi.restoreAllMocks(); view.allowed = true; view.source = 'own'; view.effects = [] })

describe('AI 生成历法草稿', () => {
  it('项目 AI 权限控制入口，手工编辑者仍可添加月份', () => {
    const page = () => renderToStaticMarkup(createElement(CalendarDialog, {
      pid: 5, initial: calendar, onClose: vi.fn(), onChanged: vi.fn(),
    }))
    expect(page()).toContain('AI 生成历法')
    view.allowed = false
    const html = page()
    expect(html).toContain('添加月份')
    expect(html).not.toContain('AI 生成历法')
  })

  it('显式发起才调用模型；连续点击只发一次，返回结果不会自动采用或保存历法', async () => {
    view.source = 'owner'
    const slow = deferred<GeneratedCalendar>()
    const request = vi.spyOn(storyTimelineApi, 'generateCalendar').mockReturnValue(slow.promise)
    const save = vi.spyOn(storyTimelineApi, 'saveCalendar')
    const { onAdopt } = renderGenerator()
    expect(request).not.toHaveBeenCalled()
    const first = view.submit()
    await view.submit()
    expect(request).toHaveBeenCalledExactlyOnceWith(5, { expected_revision: 2, ai_config_id: null, instruction: '' }, expect.any(Object))
    slow.resolve(calendar)
    await first
    expect(onAdopt).not.toHaveBeenCalled()
    expect(save).not.toHaveBeenCalled()
  })

  it('权限撤销或无模型时即使触发提交也不请求', async () => {
    const request = vi.spyOn(storyTimelineApi, 'generateCalendar')
    renderGenerator()
    view.allowed = false
    await view.submit()
    view.allowed = true
    view.source = 'none'
    renderGenerator()
    await view.submit()
    expect(request).not.toHaveBeenCalled()
  })

  it('版本过期时读取最新历法，手工输入不被生成结果覆盖', async () => {
    vi.spyOn(storyTimelineApi, 'generateCalendar').mockRejectedValue(new ApiError(409, '日期变化', { code: 'story_timeline_changed' }))
    const newest = { ...calendar, revision: 3 }
    vi.spyOn(storyTimelineApi, 'calendar').mockResolvedValue(newest)
    const { onAdopt, onBaseChanged } = renderGenerator()
    await view.submit()
    expect(onBaseChanged).toHaveBeenCalledExactlyOnceWith(newest)
    expect(onAdopt).not.toHaveBeenCalled()
  })

  it('关闭等待中的弹窗会中止请求并忽略迟到的响应', async () => {
    const slow = deferred<GeneratedCalendar>()
    const request = vi.spyOn(storyTimelineApi, 'generateCalendar').mockReturnValue(slow.promise)
    const { onAdopt, onBaseChanged } = renderGenerator()
    const cleanups = view.effects.map((effect) => effect())
    const started = view.submit()
    cleanups.forEach((cleanup) => { if (typeof cleanup === 'function') cleanup() })
    expect(request.mock.calls[0][2].signal?.aborted).toBe(true)
    slow.resolve(calendar)
    await started
    expect(onAdopt).not.toHaveBeenCalled()
    expect(onBaseChanged).not.toHaveBeenCalled()
  })
})
