import { createElement, type DependencyList, type EffectCallback } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Task } from '../api/types'
import type { TaskOutcome } from '../hooks/useTaskStream'
import WizardGenerator from './WizardGenerator'

// SSR 只渲染一次；捕获「挂载时接上最近任务」effect 并手动执行其真实闭包，不模拟浏览器交互。
const view = vi.hoisted(() => ({
  effects: [] as { setup: EffectCallback; deps: DependencyList | undefined }[],
  follow: vi.fn<(task: Task) => Promise<TaskOutcome>>(),
  list: vi.fn<() => Promise<Task[]>>(),
  info: vi.fn(),
  success: vi.fn(),
}))
vi.mock('react', async (original) => {
  const actual = await original<typeof import('react')>()
  return { ...actual,
    useEffect: (setup: EffectCallback, deps?: DependencyList) => { view.effects.push({ setup, deps }) },
  }
})
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual, App: Object.assign(actual.App, {
    useApp: () => ({ message: { info: view.info, success: view.success, error: vi.fn(), warning: vi.fn() }, modal: { warning: vi.fn() } }),
  }) }
})
vi.mock('../hooks/useTaskStream', () => ({ useTaskStream: () => ({
  running: false, task: null, text: '', error: null, reconnecting: false, follow: view.follow, cancel: async () => {},
}) }))
vi.mock('../hooks/useHasAIConfig', () => ({ useHasAIConfig: () => true }))
vi.mock('./AIConfigSelect', () => ({ default: () => null }))
vi.mock('../stores/tasks', () => ({ useTasksStore: (select: (state: unknown) => unknown) => select({ track: vi.fn(), refresh: vi.fn() }) }))
vi.mock('../stores/project', () => ({
  useProjectStore: (select: (state: unknown) => unknown) => select({ project: null }),
  captureProjectScope: () => ({ projectId: 1 }),
  isProjectScopeCurrent: () => true,
}))
vi.mock('../api/tasks', async (original) => {
  const actual = await original<typeof import('../api/tasks')>()
  return { ...actual, taskApi: { ...actual.taskApi, list: view.list } }
})

const running: Task = { id: 7, task_key: '00000000-0000-4000-8000-000000000007', kind: 'wizard.characters', status: 'running',
  title: '角色向导', project_id: 1, chapter_id: null, progress: 0, stage: '', error: '', retry_of_id: null,
  character_id: null, outline_id: null, analysis_id: null, checkpoint: null, step_cursor: 0, queue_position: null,
  created_at: '', started_at: null, finished_at: null,
}

async function mountWith(outcome: TaskOutcome) {
  view.effects = []
  view.follow.mockResolvedValue(outcome)
  view.list.mockResolvedValue([running])
  const onDone = vi.fn()
  const onUnpublished = vi.fn()
  renderToStaticMarkup(createElement(WizardGenerator, { projectId: 1, kind: 'characters', onDone, onUnpublished }))
  const resume = view.effects.find((effect) => effect.deps?.length === 2 && effect.deps[0] === 1 && effect.deps[1] === 'characters')
  expect(resume).toBeDefined()
  resume!.setup()
  await vi.waitFor(() => expect(view.follow).toHaveBeenCalled())
  await Promise.resolve(); await Promise.resolve()
  return { onDone, onUnpublished }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('向导结果未发布', () => {
  it('提示说明并刷新对应设定，不调用 onDone（不标记步骤完成）', async () => {
    const { onDone, onUnpublished } = await mountWith({
      status: 'succeeded', task: null, result: null, unpublished: true, detail: '结果已保存，请刷新后查看',
    })
    await vi.waitFor(() => expect(onUnpublished).toHaveBeenCalledOnce())
    expect(view.info).toHaveBeenCalledWith('结果已保存，请刷新后查看')
    expect(onDone).not.toHaveBeenCalled()
    expect(view.success).not.toHaveBeenCalled()
  })

  it('正常发布：调用 onDone，不走未发布刷新', async () => {
    const { onDone, onUnpublished } = await mountWith({
      status: 'succeeded', task: null, result: { kind: 'characters', items: [] },
    })
    await vi.waitFor(() => expect(onDone).toHaveBeenCalledOnce())
    expect(onUnpublished).not.toHaveBeenCalled()
  })
})
