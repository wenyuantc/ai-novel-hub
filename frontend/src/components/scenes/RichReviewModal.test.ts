import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, invalidateRequestGeneration } from '../../api/client'
import { projectApi } from '../../api/projects'
import { richDocumentApi, type RichReviewOutcome, type RichReviewPreview } from '../../api/richDocument'
import { taskApi } from '../../api/tasks'
import type { Project, ProjectAccess, Task, User } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import RichReviewModal from './RichReviewModal'

const view = vi.hoisted(() => ({
  stateIndex: -1, states: [] as unknown[], updates: [] as unknown[],
  apply: undefined as (() => void) | undefined,
  notice: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
  pathname: '/projects/9/chapters',
  effects: [] as Array<() => void | (() => void)>,
  cleanup: undefined as (() => void) | undefined,
}))

vi.mock('react', async (original) => {
  const actual = await original<typeof import('react')>()
  return { ...actual, useState: <T,>(initial?: T | (() => T)) => {
    const [value, set] = actual.useState<T | undefined>(initial)
    const index = view.stateIndex++
    if (index >= 0 && index < 9) {
      return [index < view.states.length ? view.states[index] as T : value,
        (next: unknown) => { view.updates.push(next) }] as const
    }
    return [value, set] as const
  }, useLayoutEffect: () => {}, useEffect: (effect: () => void | (() => void), deps?: readonly unknown[]) => {
    view.effects.push(effect)
    return actual.useEffect(effect, deps)
  } }
})
vi.mock('react-router-dom', () => ({ useLocation: () => ({ pathname: view.pathname }) }))
vi.mock('../../stores/auth', async (original) => {
  const actual = await original<typeof import('../../stores/auth')>()
  const hook = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { ...actual, useAuthStore: Object.assign(hook, actual.useAuthStore) }
})
vi.mock('../../stores/project', async (original) => {
  const actual = await original<typeof import('../../stores/project')>()
  const hook = (selector: (state: ReturnType<typeof actual.useProjectStore.getState>) => unknown) => selector(actual.useProjectStore.getState())
  return { ...actual, useProjectStore: Object.assign(hook, actual.useProjectStore) }
})
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual,
    App: { useApp: () => ({ message: view.notice }) },
    Modal: (props: import('antd').ModalProps) => createElement('section', null,
      typeof props.footer === 'function' ? null : props.footer, props.children),
    Button: (props: import('antd').ButtonProps) => {
      if (props.children === '确认并采纳整章') view.apply = () => props.onClick?.({} as never)
      return createElement('button', { disabled: props.disabled }, props.children)
    },
  }
})

const task = { id: 4, task_key: '00000000-0000-4000-8000-000000000004', project_id: 9, chapter_id: 2,
  status: 'paused', rich_review: { task_key: '00000000-0000-4000-8000-000000000004', run_key: 'run-key',
    token: 'token', chapter_id: 2, phase: 'awaiting_review' } } as Task
const preview: RichReviewPreview = { reason: 'rich_document_review', status: 'pending', task_status: 'paused',
  task_key: task.task_key!, run_key: 'run-key', token: 'token', chapter_id: 2, content_revision: 1, scenes: [] }
const outcome: RichReviewOutcome = { status: 'applied', task_status: 'succeeded', chapter_id: 2, content_revision: 2,
  analysis_warning: '自动分析未排队，请检查模型设置后手动分析' }
const user: User = { id: 1, username: '编辑者', created_at: '', is_admin: false, is_super_admin: false,
  effective_permissions: [], roles: [], authorization_revision: 1 }
const editor: ProjectAccess = { role: 'editor', is_collaborator: true, authorization_version: 'grant-1', allow_owner_ai: true,
  can_read: true, can_edit_content: true, can_use_ai: true, can_export: true, can_manage_collaborators: false,
  can_recycle: false, can_backup: false, can_write_reading_progress: false }
const project = { id: 9, owner: { id: 2, username: '作者' }, access: editor } as Project

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
async function renderModal() {
  const onChanged = vi.fn(), onClose = vi.fn()
  view.stateIndex = 0; view.states = [preview, false]; view.effects = []
  renderToString(createElement(RichReviewModal, { task, onClose, onChanged }))
  expect(view.apply).toBeDefined()
  const cleanup = view.effects[0]()
  view.cleanup = typeof cleanup === 'function' ? cleanup : undefined
  await vi.waitFor(() => expect(richDocumentApi.review).toHaveBeenCalledOnce())
  await new Promise((resolve) => setTimeout(resolve, 0))
  view.updates = []
  vi.mocked(projectApi.get).mockClear()
  vi.mocked(richDocumentApi.review).mockClear().mockResolvedValue({ ...preview, status: 'applied', task_status: 'succeeded' })
  return { onChanged, onClose, click: view.apply! }
}

beforeEach(() => {
  useProjectStore.getState().reset()
  useAuthStore.setState({ status: 'authenticated', user })
  view.pathname = '/projects/9/chapters'; view.apply = undefined; view.updates = []
  view.notice.success.mockReset(); view.notice.warning.mockReset(); view.notice.error.mockReset()
  vi.spyOn(projectApi, 'get').mockResolvedValue(project)
  vi.spyOn(taskApi, 'get').mockResolvedValue({ ...task, status: 'succeeded' })
  vi.spyOn(richDocumentApi, 'review').mockResolvedValue(preview)
})
afterEach(() => { view.cleanup?.(); view.cleanup = undefined; useProjectStore.getState().reset(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('采纳富文档后的自动分析提示', () => {
  it('正文已采纳且分析未排队时展示服务端安全提示，并刷新原任务', async () => {
    vi.spyOn(richDocumentApi, 'acceptReview').mockResolvedValue(outcome)
    const ui = await renderModal(); ui.click()
    await vi.waitFor(() => expect(ui.onChanged).toHaveBeenCalledOnce())
    expect(view.notice.warning).toHaveBeenCalledOnce()
    expect(JSON.stringify(view.notice.warning.mock.calls)).toContain(outcome.analysis_warning)
    expect(view.notice.success).not.toHaveBeenCalled()
    expect(ui.onClose).toHaveBeenCalledOnce()
  })

  it('无分析提示时保持普通成功反馈', async () => {
    const normal: RichReviewOutcome = { status: 'applied', task_status: 'succeeded', chapter_id: 2, content_revision: 2 }
    vi.spyOn(richDocumentApi, 'acceptReview').mockResolvedValue(normal)
    const ui = await renderModal(); ui.click()
    await vi.waitFor(() => expect(ui.onChanged).toHaveBeenCalledOnce())
    expect(view.notice.success).toHaveBeenCalledOnce()
    expect(view.notice.warning).not.toHaveBeenCalled()
  })

  it('当前作品为编辑者授权时仍可采纳并显示提示', async () => {
    await useProjectStore.getState().load(project.id)
    vi.spyOn(richDocumentApi, 'acceptReview').mockResolvedValue(outcome)
    const ui = await renderModal(); ui.click()
    await vi.waitFor(() => expect(ui.onChanged).toHaveBeenCalledOnce())
    expect(JSON.stringify(view.notice.warning.mock.calls)).toContain(outcome.analysis_warning)
  })

  it.each([
    ['降为查看者', { ...editor, role: 'viewer' as const, can_edit_content: false, can_use_ai: false }],
    ['撤销并重新邀请', { ...editor, authorization_version: 'grant-2' }],
  ])('全局任务面板中%s后不回显旧授权结果', async (_label, access) => {
    vi.spyOn(richDocumentApi, 'acceptReview').mockResolvedValue(outcome)
    const ui = await renderModal()
    vi.mocked(projectApi.get).mockResolvedValue({ ...project, access })
    ui.click()
    await vi.waitFor(() => expect(ui.onClose).toHaveBeenCalledOnce())
    expect(taskApi.get).not.toHaveBeenCalled()
    expect(ui.onChanged).not.toHaveBeenCalled()
    expect(JSON.stringify(view.notice.warning.mock.calls)).not.toContain(outcome.analysis_warning)
  })

  it('响应未知时冻结当前操作，不自动或因重复点击再发送', async () => {
    const accept = vi.spyOn(richDocumentApi, 'acceptReview').mockRejectedValue(new Error('网络中断'))
    const ui = await renderModal(); ui.click()
    await vi.waitFor(() => expect(view.updates.some((item) => typeof item === 'string' && item.includes('无法确认本次采纳结果'))).toBe(true))
    ui.click()
    expect(accept).toHaveBeenCalledOnce()
    expect(taskApi.get).not.toHaveBeenCalled()
    expect(view.notice.success).not.toHaveBeenCalled()
    expect(view.notice.warning).not.toHaveBeenCalled()
  })

  it('采纳确认后刷新失败仍说明正文已保存和分析未排队', async () => {
    vi.spyOn(richDocumentApi, 'acceptReview').mockResolvedValue(outcome)
    vi.mocked(taskApi.get).mockRejectedValue(new Error('网络中断'))
    const ui = await renderModal(); ui.click()
    await vi.waitFor(() => expect(view.updates.some((item) => typeof item === 'string' && item.includes('候选已采纳'))).toBe(true))
    expect(view.updates).toContain(outcome.analysis_warning)
    expect(ui.onChanged).not.toHaveBeenCalled()
  })

  it('采纳后权限复查网络失败时提示核对并冻结，不回显未经复核的提示', async () => {
    vi.spyOn(richDocumentApi, 'acceptReview').mockResolvedValue(outcome)
    const ui = await renderModal()
    vi.mocked(projectApi.get).mockRejectedValue(new Error('网络中断'))
    ui.click()
    await vi.waitFor(() => expect(view.updates.some((item) => typeof item === 'string' && item.includes('无法核对当前权限'))).toBe(true))
    expect(view.updates).not.toContain(outcome.analysis_warning)
    expect(taskApi.get).not.toHaveBeenCalled()
    ui.click()
    expect(richDocumentApi.acceptReview).toHaveBeenCalledOnce()
  })

  it('切换作品后迟到的采纳响应不能刷新任务或显示旧提示', async () => {
    const pending = deferred<RichReviewOutcome>()
    vi.spyOn(richDocumentApi, 'acceptReview').mockReturnValue(pending.promise)
    const ui = await renderModal(); ui.click()
    useProjectStore.setState((state) => ({ scopeVersion: state.scopeVersion + 1 }))
    pending.resolve(outcome)
    await pending.promise
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(taskApi.get).not.toHaveBeenCalled()
    expect(ui.onChanged).not.toHaveBeenCalled()
    expect(view.notice.warning).not.toHaveBeenCalled()
  })

  it('项目授权变化后，即使旧按钮尚未重绘也不能发送采纳', async () => {
    const accept = vi.spyOn(richDocumentApi, 'acceptReview').mockResolvedValue(outcome)
    const ui = await renderModal()
    useProjectStore.setState((state) => ({ scopeVersion: state.scopeVersion + 1 }))
    ui.click()
    expect(accept).not.toHaveBeenCalled()
    expect(ui.onChanged).not.toHaveBeenCalled()
  })

  it('路由先于作品状态更新时，旧作品的采纳响应也不会落地', async () => {
    const pending = deferred<RichReviewOutcome>()
    vi.spyOn(richDocumentApi, 'acceptReview').mockReturnValue(pending.promise)
    const ui = await renderModal(); ui.click()
    vi.stubGlobal('window', { location: { pathname: '/projects/10/chapters' } })
    pending.resolve(outcome)
    await pending.promise
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(taskApi.get).not.toHaveBeenCalled()
    expect(ui.onChanged).not.toHaveBeenCalled()
    expect(view.notice.warning).not.toHaveBeenCalled()
  })

  it('同一账号重新登录后不应用上一会话的采纳响应', async () => {
    const pending = deferred<RichReviewOutcome>()
    vi.spyOn(richDocumentApi, 'acceptReview').mockReturnValue(pending.promise)
    const ui = await renderModal(); ui.click()
    invalidateRequestGeneration()
    pending.resolve(outcome)
    await pending.promise
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(taskApi.get).not.toHaveBeenCalled()
    expect(ui.onChanged).not.toHaveBeenCalled()
    expect(view.notice.warning).not.toHaveBeenCalled()
  })

  it('复查任务期间撤权时忽略迟到的任务响应', async () => {
    const pending = deferred<Task>()
    vi.spyOn(richDocumentApi, 'acceptReview').mockResolvedValue(outcome)
    vi.mocked(taskApi.get).mockReturnValue(pending.promise)
    const ui = await renderModal(); ui.click()
    await vi.waitFor(() => expect(taskApi.get).toHaveBeenCalledOnce())
    useProjectStore.setState((state) => ({ scopeVersion: state.scopeVersion + 1 }))
    pending.resolve({ ...task, status: 'succeeded' })
    await pending.promise
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(richDocumentApi.review).not.toHaveBeenCalled()
    expect(ui.onChanged).not.toHaveBeenCalled()
    expect(view.notice.warning).not.toHaveBeenCalled()
  })

  it('撤权后的复查失败不显示已失权作品的采纳提示', async () => {
    vi.spyOn(richDocumentApi, 'acceptReview').mockResolvedValue(outcome)
    vi.mocked(taskApi.get).mockRejectedValue(new ApiError(404, '任务不存在'))
    const ui = await renderModal(); ui.click()
    await vi.waitFor(() => expect(ui.onClose).toHaveBeenCalledOnce())
    expect(ui.onChanged).not.toHaveBeenCalled()
    expect(view.notice.success).not.toHaveBeenCalled()
    expect(JSON.stringify(view.notice.warning.mock.calls)).not.toContain(outcome.analysis_warning)
  })
})
