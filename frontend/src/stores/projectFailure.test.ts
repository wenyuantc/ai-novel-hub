import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, captureProjectFailureForRequest, setPermissionDeniedHandler, setUnauthorizedHandler } from '../api/client'
import { projectApi } from '../api/projects'
import { postSSE } from '../api/sse'
import { subscribeTask, taskApi } from '../api/tasks'
import { CardImportController } from '../components/entities/cardImportController'
import { SettingTagsSession } from '../components/settingTags/settingTagsController'
import type { Project, ProjectAccess, User } from '../api/types'
import { writeResultUnknown } from '../utils/writeResult'
import { useAuthStore } from './auth'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore, verifyProjectScope } from './project'

const user: User = {
  id: 1, username: '协作者', created_at: '', is_admin: false, is_super_admin: false,
  effective_permissions: [], roles: [], authorization_revision: 1,
}
const editor: ProjectAccess = {
  role: 'editor', is_collaborator: true, authorization_version: 'grant-1', allow_owner_ai: false,
  can_read: true, can_edit_content: true, can_export: true, can_use_ai: true,
  can_manage_collaborators: false, can_recycle: false, can_backup: false, can_write_reading_progress: false,
}
const viewer: ProjectAccess = { ...editor, role: 'viewer', can_edit_content: false, can_use_ai: false }

function project(id = 2, access = editor): Project {
  return {
    id, access, owner: { id: 9, username: '作者' }, title: `书${id}`, genre: '', premise: '',
    status: 'planning', created_at: '', updated_at: '', chapter_count: 0, total_words: 0,
    cover_updated_at: null, auto_analyze: true, wizard_progress: { done: [], skipped: [], current: null },
    default_style_id: null, default_style: null, synopsis: '', theme: '', tags: [], selling_points: '',
    audience: '', target_words: null, last_edited_chapter_id: null, reading: null,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function failure(status: number, code?: string): Response {
  return new Response(JSON.stringify({ detail: '请求失败', code }), { status })
}

beforeEach(() => {
  useProjectStore.getState().reset()
  useAuthStore.setState({ status: 'authenticated', user })
})
afterEach(() => {
  useProjectStore.getState().reset()
  setPermissionDeniedHandler(null)
  setUnauthorizedHandler(null)
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('作品请求失败后的权威能力刷新', () => {
  it('只读 403 刷新作品并立即收紧当前编辑与 AI 能力，不重发写请求', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockResolvedValueOnce(project(2, viewer))
    await useProjectStore.getState().load(2)
    const oldScope = captureProjectScope(2)
    const fetchMock = vi.fn(async () => failure(403, 'project_readonly'))
    vi.stubGlobal('fetch', fetchMock)

    const error = await api.patch('/projects/2/characters/7', { name: '修改' }).catch((e: unknown) => e)
    await vi.waitFor(() => expect(useProjectStore.getState().project?.access).toEqual(viewer))
    expect(error).toBeInstanceOf(ApiError)
    expect(writeResultUnknown(error)).toBe(false)
    expect(isProjectScopeCurrent(oldScope, 'canEdit')).toBe(false)
    expect(isProjectScopeCurrent(captureProjectScope(2), 'canEdit')).toBe(false)
    expect(isProjectScopeCurrent(captureProjectScope(2), 'canUseAIEntitled')).toBe(false)
    expect(get).toHaveBeenCalledTimes(2)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('子资源 404 仅确认作品权限；相同授权保持草稿作用域', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValue(project())
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    vi.stubGlobal('fetch', vi.fn(async () => failure(404)))
    await expect(api.patch('/projects/2/characters/7', {})).rejects.toMatchObject({ status: 404 })
    await vi.waitFor(() => expect(get).toHaveBeenCalledTimes(2))
    expect(isProjectScopeCurrent(scope, 'canEdit')).toBe(true)
    expect(useProjectStore.getState().project?.id).toBe(2)
  })

  it('SSE 握手 403 也刷新原作品，且不重发流式请求', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockResolvedValueOnce(project(2, viewer))
    await useProjectStore.getState().load(2)
    const fetchMock = vi.fn(async () => failure(403, 'project_readonly'))
    vi.stubGlobal('fetch', fetchMock)
    await expect(postSSE('/projects/2/chapters/3/scenes/4/rewrite', {}, { onMessage: () => {} }))
      .rejects.toMatchObject({ status: 403 })
    await vi.waitFor(() => expect(useProjectStore.getState().project?.access).toEqual(viewer))
    expect(get).toHaveBeenCalledTimes(2)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('刷新先于旧错误返回时，旧授权轮次不能刷新或触发全局权限处理', async () => {
    const nextEditor = { ...editor, authorization_version: 'grant-2' }
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockResolvedValueOnce(project(2, nextEditor))
    await useProjectStore.getState().load(2)
    const oldScope = captureProjectScope(2)
    const response = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn(() => response.promise))
    const denied = vi.fn()
    setPermissionDeniedHandler(denied)
    const pending = api.patch('/projects/2/characters/7', {}).catch((e: unknown) => e)
    await useProjectStore.getState().refresh()
    expect(isProjectScopeCurrent(oldScope)).toBe(false)
    response.resolve(failure(403, 'permission_denied'))
    expect(await pending).toMatchObject({ status: 403 })
    expect(get).toHaveBeenCalledTimes(2)
    expect(denied).not.toHaveBeenCalled()
    expect(useProjectStore.getState().project?.access).toEqual(nextEditor)
  })

  it('旧授权轮次的 401 也不能清空当前会话', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project())
      .mockResolvedValueOnce(project(2, { ...editor, authorization_version: 'grant-2' }))
    await useProjectStore.getState().load(2)
    const response = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn(() => response.promise))
    const unauthorized = vi.fn()
    setUnauthorizedHandler(unauthorized)
    const pending = api.patch('/projects/2/characters/7', {}).catch((e: unknown) => e)
    await useProjectStore.getState().refresh()
    response.resolve(failure(401))
    expect(await pending).toMatchObject({ status: 401 })
    expect(get).toHaveBeenCalledTimes(2)
    expect(unauthorized).not.toHaveBeenCalled()
  })

  it('错误响应体读取期间切换授权轮次，不能以旧 403 触发刷新', async () => {
    const next = { ...editor, authorization_version: 'grant-2' }
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockResolvedValueOnce(project(2, next))
    await useProjectStore.getState().load(2)
    let body!: ReadableStreamDefaultController<Uint8Array>
    const response = new Response(new ReadableStream<Uint8Array>({ start(controller) { body = controller } }), { status: 403 })
    vi.stubGlobal('fetch', vi.fn(async () => response))
    const denied = vi.fn()
    setPermissionDeniedHandler(denied)
    const pending = api.patch('/projects/2/characters/7', {}).catch((e: unknown) => e)
    await Promise.resolve(); await Promise.resolve()
    await useProjectStore.getState().refresh()
    body.enqueue(new TextEncoder().encode('{"code":"permission_denied"}')); body.close()
    expect(await pending).toMatchObject({ status: 403 })
    expect(denied).not.toHaveBeenCalled()
    expect(get).toHaveBeenCalledTimes(2)
    expect(useProjectStore.getState().project?.access).toEqual(next)
  })

  it('同账号退出再登录及切作品后，旧 404 不能刷新或清除新作用域', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockResolvedValueOnce(project())
      .mockResolvedValueOnce(project(3))
    await useProjectStore.getState().load(2)
    const oldSession = deferred<Response>()
    const oldProject = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(oldSession.promise).mockReturnValueOnce(oldProject.promise))
    const first = api.patch('/projects/2/characters/7', {}).catch((e: unknown) => e)
    useAuthStore.getState().clear()
    useAuthStore.setState({ status: 'authenticated', user })
    await useProjectStore.getState().load(2)
    const second = api.patch('/projects/2/characters/7', {}).catch((e: unknown) => e)
    await useProjectStore.getState().load(3)
    const current = captureProjectScope(3)
    oldSession.resolve(failure(404)); oldProject.resolve(failure(404))
    expect(await first).toMatchObject({ status: 404 })
    expect(await second).toMatchObject({ status: 404 })
    expect(get).toHaveBeenCalledTimes(3)
    expect(isProjectScopeCurrent(current, 'canEdit')).toBe(true)
    expect(useProjectStore.getState().project?.id).toBe(3)
  })

  it.each([403, 404])('只有详情刷新确认 %i 才撤下作品，且不递归', async (status) => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(project()), { status: 200 }))
      .mockResolvedValueOnce(failure(404))
      .mockResolvedValueOnce(failure(status))
    vi.stubGlobal('fetch', fetchMock)
    await useProjectStore.getState().load(2)
    await expect(api.patch('/projects/2/characters/7', {})).rejects.toMatchObject({ status: 404 })
    await vi.waitFor(() => expect(useProjectStore.getState().project).toBeNull())
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      '/api/v1/projects/2', '/api/v1/projects/2/characters/7', '/api/v1/projects/2',
    ])
  })

  it('确认作品的网络失败保留能力和草稿作用域', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockRejectedValueOnce(new ApiError(0, '网络错误'))
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    vi.stubGlobal('fetch', vi.fn(async () => failure(404)))
    await expect(api.patch('/projects/2/characters/7', {})).rejects.toMatchObject({ status: 404 })
    await vi.waitFor(() => expect(get).toHaveBeenCalledTimes(2))
    expect(isProjectScopeCurrent(scope, 'canEdit')).toBe(true)
  })

  it('草稿复查等待已在途的权威刷新，不互相覆盖或重复读取', async () => {
    const slow = deferred<Project>()
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockReturnValueOnce(slow.promise)
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    vi.stubGlobal('fetch', vi.fn(async () => failure(404)))
    await expect(api.patch('/projects/2/characters/7', {})).rejects.toMatchObject({ status: 404 })
    const verified = verifyProjectScope(scope)
    expect(get).toHaveBeenCalledTimes(2)
    slow.resolve(project())
    expect(await verified).toBe('confirmed')
    expect(get).toHaveBeenCalledTimes(2)
    expect(isProjectScopeCurrent(scope, 'canEdit')).toBe(true)
  })

  it('草稿复查遇到网络错误返回未知并保留原作用域', async () => {
    const slow = deferred<Project>()
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockReturnValueOnce(slow.promise)
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    vi.stubGlobal('fetch', vi.fn(async () => failure(403, 'project_readonly')))
    await expect(api.patch('/projects/2/characters/7', {})).rejects.toMatchObject({ status: 403 })
    const verified = verifyProjectScope(scope)
    slow.reject(new ApiError(0, '断网'))
    expect(await verified).toBe('unknown')
    expect(get).toHaveBeenCalledTimes(2)
    expect(isProjectScopeCurrent(scope, 'canEdit')).toBe(true)
  })

  it('权威刷新已因断网完成时，初次冻结不自动重试；用户重查才再次读取', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockRejectedValueOnce(new ApiError(0, '断网'))
      .mockResolvedValueOnce(project())
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    vi.stubGlobal('fetch', vi.fn(async () => failure(404)))
    await expect(api.patch('/projects/2/characters/7', {})).rejects.toMatchObject({ status: 404 })
    await vi.waitFor(() => expect(get).toHaveBeenCalledTimes(2))
    expect(await verifyProjectScope(scope, true)).toBe('unknown')
    expect(get).toHaveBeenCalledTimes(2)
    expect(await verifyProjectScope(scope)).toBe('confirmed')
    expect(get).toHaveBeenCalledTimes(3)
  })

  it('真实卡片 API 的子资源 404 与作品刷新并发时保留映射，只读一次权威作品', async () => {
    const slow = deferred<Project>()
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockReturnValueOnce(slow.promise)
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    const preview = { fields_revision: 1, categories_revision: 0, ai_visibility_revision: 0, can_import: true,
      field_definitions: [], references: [], characters: [{ name: '林远', status: 'new', rename_to: null }],
      organizations: [], careers: [], relationships: [], warnings: [] }
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(preview), { status: 200 }))
      .mockResolvedValueOnce(failure(404))
    vi.stubGlobal('fetch', fetchMock)
    const source = { format: 'ainovel.cards', version: 4, characters: [{ ref: 'local', name: '林远', fields: [] }], organizations: [], careers: [] }
    const controller = new CardImportController(2, undefined, () => isProjectScopeCurrent(scope), () => {},
      () => isProjectScopeCurrent(scope, 'canEdit'), (afterFailure) => verifyProjectScope(scope, afterFailure))
    controller.activate()
    await controller.selectFile({ name: '卡片.json', size: 100, text: async () => JSON.stringify(source) })
    controller.setReference('outside', { reference: { kind: 'world', id: 7 }, type: 'world.place', name: '青山', category: 'place' })
    await controller.refreshPreview()
    expect(controller.getSnapshot()).toMatchObject({ checkingAccess: true, accessUncertain: true,
      mappings: { references: { outside: { kind: 'world', id: 7 } } } })
    expect(get).toHaveBeenCalledTimes(2)
    slow.resolve(project())
    await vi.waitFor(() => expect(controller.getSnapshot().accessUncertain).toBe(false))
    await controller.confirm()
    expect(controller.getSnapshot().reviewRequired).toBe(true)
    expect(get).toHaveBeenCalledTimes(2)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    controller.deactivate()
  })

  it('真实标签写入 403 后等待同一作品复查，草稿保留且不自动重发', async () => {
    const slow = deferred<Project>()
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockReturnValueOnce(slow.promise)
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    const entry = { kind: 'character' as const, id: 7 }
    const snapshot = { categories_revision: 1, setting_tags: ['原标签'],
      entry: { reference: entry, name: '阿青', type: 'character', category: null } }
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(snapshot), { status: 200 }))
      .mockResolvedValueOnce(failure(403, 'project_readonly'))
    vi.stubGlobal('fetch', fetchMock)
    const session = new SettingTagsSession(2, undefined, () => ({ identity: 'same', owner: 'same',
      canRead: isProjectScopeCurrent(scope), canEdit: isProjectScopeCurrent(scope, 'canEdit') }), () => {},
    (_identity, afterFailure) => verifyProjectScope(scope, afterFailure))
    const controller = session.get(entry)
    await controller.open()
    controller.edit(['我的草稿'])
    await controller.save()
    expect(controller.getSnapshot()).toMatchObject({ accessUncertain: true, checkingAccess: true, draft: ['我的草稿'] })
    expect(get).toHaveBeenCalledTimes(2)
    slow.resolve(project())
    await vi.waitFor(() => expect(controller.getSnapshot().accessUncertain).toBe(false))
    await controller.save()
    expect(controller.getSnapshot().reviewRequired).toBe(true)
    expect(get).toHaveBeenCalledTimes(2)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    session.deactivate()
  })

  it('写入结果未知不被当作撤权；403/404 则是已知失败并单独复读', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValue(project())
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    const fetchMock = vi.fn().mockRejectedValueOnce(new TypeError('offline')).mockResolvedValueOnce(failure(500))
    vi.stubGlobal('fetch', fetchMock)
    const networkError = await api.patch('/projects/2/characters/7', {}).catch((e: unknown) => e)
    const serverError = await api.patch('/projects/2/characters/7', {}).catch((e: unknown) => e)
    expect(writeResultUnknown(networkError)).toBe(true)
    expect(writeResultUnknown(serverError)).toBe(true)
    expect(writeResultUnknown(new ApiError(403, '只读'))).toBe(false)
    expect(writeResultUnknown(new ApiError(404, '未找到'))).toBe(false)
    expect(get).toHaveBeenCalledTimes(1)
    expect(isProjectScopeCurrent(scope, 'canEdit')).toBe(true)
  })

  it('刷新期间再次发生失败，在刷新完成后再确认一次，避免漏掉后发生的撤权', async () => {
    const slow = deferred<Project>()
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(project(2, viewer))
    await useProjectStore.getState().load(2)
    const fetchMock = vi.fn(async () => failure(404))
    vi.stubGlobal('fetch', fetchMock)
    await expect(api.patch('/projects/2/characters/7', {})).rejects.toMatchObject({ status: 404 })
    await expect(api.patch('/projects/2/characters/8', {})).rejects.toMatchObject({ status: 404 })
    expect(get).toHaveBeenCalledTimes(2)
    slow.resolve(project())
    await vi.waitFor(() => expect(useProjectStore.getState().project?.access).toEqual(viewer))
    expect(get).toHaveBeenCalledTimes(3)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('同作用域的成功写响应抢先落地时，失败触发的读取被覆盖后仍会重新确认', async () => {
    const slow = deferred<Project>()
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(project(2, viewer))
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    vi.stubGlobal('fetch', vi.fn(async () => failure(403, 'project_readonly')))
    await expect(api.patch('/projects/2/characters/7', {})).rejects.toMatchObject({ status: 403 })
    useProjectStore.getState().setProject({ ...project(), title: '其他请求成功' }, scope)
    slow.resolve(project(2, viewer))
    await vi.waitFor(() => expect(useProjectStore.getState().project?.access).toEqual(viewer))
    expect(get).toHaveBeenCalledTimes(3)
    expect(isProjectScopeCurrent(scope)).toBe(false)
  })

  it('章节接口仅绑定当前明确匹配的作品章节页面，离开页面后旧请求无效', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValue(project())
    await useProjectStore.getState().load(2)
    const location = { pathname: '/projects/2/chapters/3' }
    vi.stubGlobal('window', { location })
    const slow = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(failure(403)).mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(failure(404)))
    await expect(api.post('/chapters/3/generate', {})).rejects.toMatchObject({ status: 403 })
    await vi.waitFor(() => expect(get).toHaveBeenCalledTimes(2))
    const pending = api.post('/chapters/3/polish', {}).catch((e: unknown) => e)
    location.pathname = '/projects/2/chapters/4'
    slow.resolve(failure(404))
    expect(await pending).toMatchObject({ status: 404 })
    await expect(api.post('/chapters/3/generate', {})).rejects.toMatchObject({ status: 404 })
    expect(get).toHaveBeenCalledTimes(2)
  })

  it('账户、系列、管理员与不同作品的失败不触发当前作品刷新', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValue(project())
    await useProjectStore.getState().load(2)
    vi.stubGlobal('fetch', vi.fn(async () => failure(404)))
    for (const path of ['/settings/ai', '/series/2', '/admin/projects/2', '/projects/3/characters/7']) {
      await expect(api.patch(path, {})).rejects.toMatchObject({ status: 404 })
    }
    expect(get).toHaveBeenCalledTimes(1)
    expect(useProjectStore.getState().project?.id).toBe(2)
  })

  it('SSE 旧轮次握手错误与流内错误都不刷新或重发', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project())
      .mockResolvedValueOnce(project(2, { ...editor, authorization_version: 'grant-2' }))
    await useProjectStore.getState().load(2)
    const slow = deferred<Response>()
    const fetchMock = vi.fn().mockReturnValueOnce(slow.promise).mockResolvedValueOnce(new Response(
      'event: error\ndata: {"detail":"生成失败"}\n\n', { headers: { 'content-type': 'text/event-stream' } },
    ))
    vi.stubGlobal('fetch', fetchMock)
    const pending = postSSE('/projects/2/chapters/3/scenes/4/rewrite', {}, { onMessage: () => {} }).catch((e: unknown) => e)
    await useProjectStore.getState().refresh()
    slow.resolve(failure(403, 'project_readonly'))
    expect(await pending).toMatchObject({ status: 403 })
    await expect(postSSE('/projects/2/chapters/3/scenes/4/rewrite', {}, { onMessage: () => {} })).resolves.toBeUndefined()
    expect(get).toHaveBeenCalledTimes(2)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('已绑定作品的任务流握手 404 清理任务输出并复查作品，不重连', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockResolvedValueOnce(project(2, viewer))
    await useProjectStore.getState().load(2)
    const failureScope = captureProjectFailureForRequest('/projects/2/tasks')
    const clear = vi.fn()
    const fetchMock = vi.fn(async () => failure(404))
    vi.stubGlobal('fetch', fetchMock)
    await expect(subscribeTask({ id: 7, task_key: '00000000-0000-4000-8000-000000000007' }, { onProjectAccessDenied: clear }, undefined, { projectFailure: failureScope }))
      .rejects.toMatchObject({ status: 404 })
    await vi.waitFor(() => expect(useProjectStore.getState().project?.access).toEqual(viewer))
    expect(clear).toHaveBeenCalledOnce()
    expect(get).toHaveBeenCalledTimes(2)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('任务流中的 permission_denied（详情不可读）清理输出并复查作品，不接收后续消息', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockResolvedValueOnce(project(2, viewer))
    await useProjectStore.getState().load(2)
    const detail = vi.spyOn(taskApi, 'get').mockRejectedValue(new ApiError(404, '任务不存在'))
    const failureScope = captureProjectFailureForRequest('/projects/2/tasks')
    const clear = vi.fn()
    const delta = vi.fn()
    const fetchMock = vi.fn(async () => new Response(
      'event: delta\ndata: {"text":"旧输出"}\n\nevent: error\ndata: {"code":"permission_denied","detail":"权限已变化"}\n\nevent: delta\ndata: {"text":"不得展示"}\n\n',
      { headers: { 'content-type': 'text/event-stream' } },
    ))
    vi.stubGlobal('fetch', fetchMock)
    await expect(subscribeTask({ id: 7, task_key: '00000000-0000-4000-8000-000000000007' }, { onProjectAccessDenied: clear, onDelta: delta }, undefined, { projectFailure: failureScope }))
      .rejects.toMatchObject({ status: 403 })
    await vi.waitFor(() => expect(useProjectStore.getState().project?.access).toEqual(viewer))
    expect(delta).toHaveBeenCalledExactlyOnceWith('旧输出')
    expect(clear).toHaveBeenCalledOnce()
    expect(get).toHaveBeenCalledTimes(2)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(detail).toHaveBeenCalledOnce()
  })

  it('旧授权轮次的任务握手失败不能清理或刷新新轮次', async () => {
    const next = { ...editor, authorization_version: 'grant-2' }
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockResolvedValueOnce(project(2, next))
    await useProjectStore.getState().load(2)
    const failureScope = captureProjectFailureForRequest('/projects/2/tasks')
    const clear = vi.fn()
    const slow = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn(() => slow.promise))
    const pending = subscribeTask({ id: 7, task_key: '00000000-0000-4000-8000-000000000007' }, { onProjectAccessDenied: clear }, undefined, { projectFailure: failureScope }).catch((e: unknown) => e)
    await useProjectStore.getState().refresh()
    slow.resolve(failure(403, 'permission_denied'))
    await pending
    expect(clear).not.toHaveBeenCalled()
    expect(get).toHaveBeenCalledTimes(2)
    expect(useProjectStore.getState().project?.access).toEqual(next)
  })
})
