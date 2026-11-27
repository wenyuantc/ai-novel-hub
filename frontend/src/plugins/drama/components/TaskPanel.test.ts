import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import TaskPanel, { type RunDramaTask } from './TaskPanel'
import type { Access } from './PageScope'
import { dramaApi } from '../api'
import type { Task } from '../../../api/types'

const state = vi.hoisted(() => ({
  config: true,
  running: false,
  text: '',
  follow: vi.fn(),
  cancel: vi.fn(),
  refresh: vi.fn(),
}))
vi.mock('../../../hooks/useHasAIConfig', () => ({ useHasAIConfig: () => state.config }))
vi.mock('../../../hooks/useTaskStream', () => ({
  useTaskStream: () => ({
    task: { title: '改写剧本', progress: 42 },
    running: state.running,
    text: state.text,
    follow: state.follow,
    cancel: state.cancel,
    reset: () => {},
  }),
}))
function panel(edit = true, allowed = true) {
  let run: RunDramaTask | undefined
  const access = { canEdit: edit, canUseAI: allowed, canEditNow: () => edit, canUseAINow: () => allowed } as Access
  const html = renderToStaticMarkup(
    createElement(
      MemoryRouter,
      null,
      createElement(TaskPanel, {
        sid: 4,
        eid: 7,
        access,
        refresh: state.refresh,
        children: (start) => {
          run = start
          return start ? createElement('button', null, '改写') : null
        },
      }),
    ),
  )
  return { html, run }
}
beforeEach(() => {
  vi.resetAllMocks()
  state.config = true
  state.running = false
  state.text = ''
})
afterEach(() => vi.restoreAllMocks())
describe('集工作台 AI 任务', () => {
  it('只读和无模型状态隐藏生成入口，缺模型时提供配置链接', () => {
    expect(panel(false, false).run).toBeUndefined()
    state.config = false
    const { html, run } = panel()
    expect(run).toBeUndefined()
    expect(html).toContain('/settings/ai')
  })
  it('订阅创建返回的任务身份；订阅完成后重读已保存的业务数据', async () => {
    const task = { id: 15, task_key: 'task-instance' } as Task
    const start = vi.spyOn(dramaApi, 'task').mockResolvedValue(task)
    state.follow.mockResolvedValue({ status: 'succeeded', result: { applied: true } })
    await panel().run!('drama.rewrite')
    expect(start).toHaveBeenCalledWith(4, 7, 'drama.rewrite', {})
    expect(state.follow).toHaveBeenCalledWith(task)
    expect(state.refresh).toHaveBeenCalledOnce()
  })
  it('展示实时剧本与取消入口，取消终态不刷新作品内容', async () => {
    state.running = true
    state.text = '## S1 | 渡口'
    const { html, run } = panel()
    expect(html).toContain('## S1 | 渡口')
    expect(html).toContain('取消任务')
    vi.spyOn(dramaApi, 'task').mockResolvedValue({ id: 1 } as Task)
    state.follow.mockResolvedValue({ status: 'cancelled' })
    await run!('drama.rewrite')
    expect(state.refresh).not.toHaveBeenCalled()
  })
})
