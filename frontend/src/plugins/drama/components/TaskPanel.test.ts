import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import TaskPanel, { type RunDramaTask } from './TaskPanel'
import type { Access } from './PageScope'
import { dramaApi } from '../api'
import type { EpisodeModelBinding } from '../useEpisodeModelSettings'
import type { Task } from '../../../api/types'

const state = vi.hoisted(() => ({
  config: true,
  imageSource: 'own',
  running: false,
  text: '',
  follow: vi.fn(),
  cancel: vi.fn(),
  refresh: vi.fn(),
}))
vi.mock('../../../hooks/useAIModelSource', () => ({ useAIModelSource: () => state.imageSource }))
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
function panel(edit = true, allowed = true, modelBinding?: EpisodeModelBinding) {
  let run: RunDramaTask | undefined
  let generate: ((id: number) => void) | undefined
  const access = { canEdit: edit, canUseAI: allowed, canEditNow: () => edit, canUseAINow: () => allowed } as Access
  const html = renderToStaticMarkup(
    createElement(
      MemoryRouter,
      null,
      createElement(TaskPanel, {
        sid: 4,
        modelBinding,
        eid: 7,
        access,
        refresh: state.refresh,
        children: (start, image) => {
          generate = image?.generate
          run = start
          return start ? createElement('button', null, '改写') : null
        },
      }),
    ),
  )
  return { html, run, generate }
}
beforeEach(() => {
  vi.resetAllMocks()
  state.config = true
  state.imageSource = 'own'
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
  it('只有图片模型也可提交造型任务，明确把任务发给当前集', async () => {
    state.config = false
    const task = { id: 21, task_key: 'image-task' } as Task
    const start = vi.spyOn(dramaApi, 'task').mockResolvedValue(task)
    state.follow.mockResolvedValue({ status: 'succeeded', result: {} })
    const { run, generate } = panel()
    expect(run).toBeUndefined()
    expect(generate).toBeDefined()
    await generate!(9)
    expect(start).toHaveBeenCalledWith(4, 7, 'drama.image', { targets: [{ type: 'look', id: 9 }] })
    expect(state.follow).toHaveBeenCalledWith(task)
    state.imageSource = 'none'
    expect(panel().generate).toBeUndefined()
    expect(panel(false, false).generate).toBeUndefined()
  })
  it('剧集顶部文本选择用于全部文字任务，生图使用图片选择', async () => {
    const post = vi.spyOn(dramaApi, 'task').mockResolvedValue({ id: 3, task_key: 'task-key' } as Task)
    state.follow.mockResolvedValue({ status: 'succeeded', result: {} })
    const settings: EpisodeModelBinding = {
      ready: true,
      configFor: (type) => ({ text: 11, image: 22, video: 33 })[type],
    }
    const { run, generate } = panel(true, true, settings)
    for (const kind of ['drama.rewrite', 'drama.extract', 'drama.storyboard', 'drama.prompts'] as const) {
      await run!(kind)
      expect(post).toHaveBeenLastCalledWith(4, 7, kind, { ai_config_id: 11 })
    }
    await run!('drama.prompts', { targets: [{ type: 'shot', id: 9 }] })
    expect(post).toHaveBeenLastCalledWith(4, 7, 'drama.prompts', {
      targets: [{ type: 'shot', id: 9 }],
      ai_config_id: 11,
    })
    await generate!(9)
    expect(post).toHaveBeenLastCalledWith(4, 7, 'drama.image', { targets: [{ type: 'look', id: 9 }], ai_config_id: 22 })
  })
  it('清空选择保留默认；读取、保存开始和授权失效后不提交任务', async () => {
    const post = vi.spyOn(dramaApi, 'task').mockResolvedValue({ id: 3, task_key: 'task-key' } as Task)
    state.follow.mockResolvedValue({ status: 'succeeded', result: {} })
    expect(panel(true, true, { ready: false, configFor: () => undefined }).run).toBeUndefined()
    await panel(true, true, { ready: true, configFor: () => undefined }).run!('drama.rewrite')
    expect(post).not.toHaveBeenCalled()
    await panel(true, true, { ready: true, configFor: () => null }).run!('drama.rewrite')
    expect(JSON.parse(JSON.stringify(post.mock.calls[0][3]))).toEqual({})
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
