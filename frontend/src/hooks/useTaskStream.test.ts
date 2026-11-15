import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { projectApi } from '../api/projects'
import { ApiError } from '../api/client'
import { taskApi } from '../api/tasks'
import type { Project, Task, User } from '../api/types'
import { useAuthStore } from '../stores/auth'
import { useProjectStore } from '../stores/project'
import { useTaskStream } from './useTaskStream'

// 保留真实 store 和网络栈，记录 hook 的状态写入与 effect 清理。
const hooks = vi.hoisted(() => ({ values: [] as unknown[], index: 0, effects: [] as Array<() => void | (() => void)> }))
vi.mock('react', async (original) => {
  const react = await original<typeof import('react')>()
  return {
    ...react,
    useState: (initial: unknown) => {
      const index = hooks.index++
      hooks.values[index] = typeof initial === 'function' ? (initial as () => unknown)() : initial
      return [hooks.values[index], (value: unknown) => {
        hooks.values[index] = typeof value === 'function' ? (value as (old: unknown) => unknown)(hooks.values[index]) : value
      }]
    },
    useRef: (value: unknown) => ({ current: value }),
    useCallback: (callback: unknown) => callback,
    useEffect: (effect: () => void | (() => void)) => { hooks.effects.push(effect) },
  }
})

const user: User = { id: 1, username: '作者', created_at: '', is_admin: false, is_super_admin: false,
  effective_permissions: [], roles: [], authorization_revision: 1 }
const project: Project = {
  id: 2, owner: { id: 1, username: '作者' }, title: '作品', genre: '', premise: '', status: 'planning',
  created_at: '', updated_at: '', chapter_count: 0, total_words: 0, cover_updated_at: null, auto_analyze: true,
  wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null, default_style: null,
  synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null, last_edited_chapter_id: null,
  reading: null, access: { role: 'owner', is_collaborator: false, authorization_version: null, allow_owner_ai: false,
    can_read: true, can_edit_content: true, can_export: true, can_use_ai: true, can_manage_collaborators: true,
    can_recycle: true, can_backup: true, can_write_reading_progress: true },
}
const task = { id: 7, task_key: '00000000-0000-4000-8000-000000000007', project_id: 2, status: 'running', title: '任务' } as Task

function frames() {
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({ start(value) { controller = value } })
  return { response: new Response(body, { headers: { 'content-type': 'text/event-stream' } }),
    send: (value: string) => controller.enqueue(new TextEncoder().encode(value)) }
}

beforeEach(() => {
  hooks.values = []; hooks.index = 0; hooks.effects = []
  useProjectStore.getState().reset()
  useAuthStore.setState({ status: 'authenticated', user })
})
afterEach(() => {
  useProjectStore.getState().reset()
  vi.unstubAllGlobals(); vi.restoreAllMocks()
})

describe('任务流作品失效', () => {
  it('读取中的权限收紧会主动清空已接收输出并断开本地订阅，不取消服务端任务', async () => {
    const restricted = { ...project, access: { ...project.access, can_edit_content: false, can_use_ai: false } }
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project).mockResolvedValueOnce(restricted)
    await useProjectStore.getState().load(2)
    const cancel = vi.spyOn(taskApi, 'cancel')
    const stream = frames()
    const fetchMock = vi.fn(async () => stream.response)
    vi.stubGlobal('fetch', fetchMock)
    const hook = useTaskStream()
    const cleanup = hooks.effects.map((effect) => effect())
    const result = hook.follow(task)
    stream.send('event: delta\ndata: {"text":"敏感输出"}\n\n')
    await vi.waitFor(() => expect(hooks.values[1]).toBe('敏感输出'))
    await useProjectStore.getState().refresh()
    expect(await result).toEqual({ status: 'detached' })
    expect(hooks.values.slice(0, 6)).toEqual([null, '', false, null, null, false])
    expect(fetchMock).toHaveBeenCalledOnce()
    expect(cancel).not.toHaveBeenCalled()
    cleanup.forEach((dispose) => { if (typeof dispose === 'function') dispose() })
  })

  it('流内 permission_denied 且详情 404 时清除输出并复查作品；个人任务仍保留原有错误语义', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValue(project)
    await useProjectStore.getState().load(2)
    // 详情不可读：不重订阅，按原错误处理
    const detail = vi.spyOn(taskApi, 'get').mockRejectedValue(new ApiError(404, '任务不存在'))
    const first = frames()
    const second = frames()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(first.response).mockResolvedValueOnce(second.response))
    const hook = useTaskStream()
    const cleanup = hooks.effects.map((effect) => effect())
    const result = hook.follow(task)
    first.send('event: delta\ndata: {"text":"旧输出"}\n\nevent: error\ndata: {"code":"permission_denied","detail":"权限已变化"}\n\n')
    expect(await result).toMatchObject({ status: 'lost' })
    expect(hooks.values[0]).toBeNull()
    expect(hooks.values[1]).toBe('')
    await vi.waitFor(() => expect(get).toHaveBeenCalledTimes(2))

    const personal = hook.follow({ ...task, project_id: null })
    second.send('event: delta\ndata: {"text":"个人输出"}\n\nevent: error\ndata: {"code":"permission_denied","detail":"个人错误"}\n\n')
    expect(await personal).toMatchObject({ status: 'failed', detail: '个人错误' })
    expect(hooks.values[1]).toBe('个人输出')
    expect(get).toHaveBeenCalledTimes(2)
    expect(detail).toHaveBeenCalledTimes(2)
    cleanup.forEach((dispose) => { if (typeof dispose === 'function') dispose() })
  })

  it('同 ID 新 task_key 的流事件不覆盖任务，清除旧输出且不重连', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project)
    await useProjectStore.getState().load(2)
    const stream = frames()
    const fetch = vi.fn(async () => stream.response)
    vi.stubGlobal('fetch', fetch)
    const hook = useTaskStream()
    const cleanup = hooks.effects.map((effect) => effect())
    const result = hook.follow(task)
    stream.send('event: delta\ndata: {"text":"旧输出"}\n\n')
    await vi.waitFor(() => expect(hooks.values[1]).toBe('旧输出'))
    stream.send('event: snapshot\ndata: {"task":{"id":7,"task_key":"00000000-0000-4000-8000-000000000008","status":"running"},"text":"新任务输出"}\n\n')
    expect(await result).toMatchObject({ status: 'lost' })
    expect(hooks.values[0]).toBeNull()
    expect(hooks.values[1]).toBe('')
    expect(fetch).toHaveBeenCalledOnce()
    cleanup.forEach((dispose) => { if (typeof dispose === 'function') dispose() })
  })

  it('缺任务 key 时不发起订阅并显示明确状态', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const hook = useTaskStream()
    const cleanup = hooks.effects.map((effect) => effect())
    expect(await hook.follow({ ...task, task_key: null })).toMatchObject({ status: 'lost', message: expect.stringContaining('身份') })
    expect(fetch).not.toHaveBeenCalled()
    expect(hooks.values[0]).toBeNull()
    cleanup.forEach((dispose) => { if (typeof dispose === 'function') dispose() })
  })
})
