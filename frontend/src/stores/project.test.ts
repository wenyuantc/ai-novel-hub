import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, invalidateRequestGeneration } from '../api/client'
import { projectApi } from '../api/projects'
import type { Project, ProjectAccess, User } from '../api/types'
import { useAuthStore } from './auth'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore } from './project'

const user = (over: Partial<User> = {}): User => ({
  id: 1, username: '作者', created_at: '', is_admin: false, is_super_admin: false,
  effective_permissions: [], roles: [], authorization_revision: 1, ...over,
})
const baseAccess: ProjectAccess = {
  role: 'owner', is_collaborator: false, authorization_version: null, allow_owner_ai: false, can_read: true, can_edit_content: true,
  can_export: true, can_use_ai: true, can_manage_collaborators: true, can_recycle: true,
  can_backup: true, can_write_reading_progress: true,
}
const viewer: ProjectAccess = {
  ...baseAccess, role: 'viewer', is_collaborator: true, authorization_version: 'grant-1', can_edit_content: false, can_use_ai: false,
  can_manage_collaborators: false, can_recycle: false, can_backup: false, can_write_reading_progress: false,
}
const editor: ProjectAccess = { ...viewer, role: 'editor', can_edit_content: true, can_use_ai: true }
const adminViewer: ProjectAccess = { ...viewer, can_edit_content: true, can_recycle: true }

function project(id: number, access: ProjectAccess = baseAccess): Project {
  return { id, access, owner: { id: access.role === 'owner' ? 1 : 2, username: '作者' }, title: `书${id}`,
    genre: '', premise: '', status: 'planning', created_at: '', updated_at: '', chapter_count: 0, total_words: 0,
    cover_updated_at: null, auto_analyze: true, wizard_progress: { done: [], skipped: [], current: null },
    default_style_id: null, default_style: null, synopsis: '', theme: '', tags: [], selling_points: '', audience: '',
    target_words: null, last_edited_chapter_id: null, reading: null }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

beforeEach(() => {
  useProjectStore.getState().reset()
  useAuthStore.setState({ status: 'authenticated', user: user() })
})
afterEach(() => { useProjectStore.getState().reset(); vi.restoreAllMocks() })

describe('项目能力快照与异步作用域', () => {
  it('接受服务端的协作者能力，而非把他人项目当作无权限', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project(2, editor))
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    expect(useProjectStore.getState().project?.access).toEqual(editor)
    expect(isProjectScopeCurrent(scope, 'canEdit')).toBe(true)
    expect(isProjectScopeCurrent(scope, 'canUseAIEntitled')).toBe(true)
    expect(isProjectScopeCurrent(scope, 'canUseAI')).toBe(true)
    expect(isProjectScopeCurrent(scope, 'canUseOwnerAI')).toBe(false)
    expect(isProjectScopeCurrent(scope, 'canBackup')).toBe(false)
  })

  it('作者只关闭作者模型授权：刷新即更新快照并递增作用域，模型来源随之变为不可用', async () => {
    const granted: ProjectAccess = { ...editor, allow_owner_ai: true }
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project(2, granted)).mockResolvedValueOnce(project(2, editor))
    await useProjectStore.getState().load(2)
    const before = useProjectStore.getState().scopeVersion
    const { aiModelSource, projectAccess } = await import('../utils/projectAccess')
    const source = () => aiModelSource(projectAccess(useProjectStore.getState().project, useAuthStore.getState().user, 2), true, 0)
    expect(source()).toBe('owner')
    await useProjectStore.getState().refresh()
    expect(useProjectStore.getState().project?.access.allow_owner_ai).toBe(false)
    expect(useProjectStore.getState().scopeVersion).toBeGreaterThan(before)
    expect(source()).toBe('none')
  })

  it('拒绝缺失 access、明确不可读、错误项目或与 actor 不符的作者响应', async () => {
    const get = vi.spyOn(projectApi, 'get')
      .mockResolvedValueOnce({ ...project(1), access: undefined } as unknown as Project)
      .mockResolvedValueOnce(project(1, { ...baseAccess, can_read: false }))
      .mockResolvedValueOnce(project(2))
      .mockResolvedValueOnce({ ...project(1), owner: { id: 9, username: '其他用户' } })
    for (let i = 0; i < 4; i++) {
      await useProjectStore.getState().load(1)
      expect(useProjectStore.getState().project).toBeNull()
    }
    expect(get).toHaveBeenCalledTimes(4)
  })

  it('切换项目、登出、同账号重新登录后旧请求及旧 scope 均不可复活', async () => {
    const slow = deferred<Project>()
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project(1)).mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(project(1))
    await useProjectStore.getState().load(1)
    const oldScope = captureProjectScope(1)
    const pending = useProjectStore.getState().load(2)
    expect(isProjectScopeCurrent(oldScope)).toBe(false)
    useAuthStore.setState({ status: 'anonymous', user: null })
    useAuthStore.setState({ status: 'authenticated', user: user() })
    await useProjectStore.getState().load(1)
    slow.resolve(project(2, editor)); await pending
    expect(useProjectStore.getState().project?.id).toBe(1)
    useProjectStore.getState().setProject({ ...project(1), title: '旧会话结果' }, oldScope)
    expect(useProjectStore.getState().project?.title).toBe('书1')
  })

  it('同 actor 权限变化立刻使旧响应失效，管理员查看者保留只读并刷新', async () => {
    const admin = user({ effective_permissions: ['projects.read', 'projects.edit', 'projects.recycle'] })
    useAuthStore.setState({ user: admin })
    const slow = deferred<Project>()
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project(2, adminViewer)).mockReturnValueOnce(slow.promise)
    await useProjectStore.getState().load(2)
    const oldScope = captureProjectScope(2)
    useAuthStore.setState({ user: { ...admin, authorization_revision: 2, effective_permissions: [] } })
    expect(isProjectScopeCurrent(oldScope)).toBe(false)
    const current = captureProjectScope(2)
    expect(isProjectScopeCurrent(current, 'canRead')).toBe(true)
    expect(isProjectScopeCurrent(current, 'canEdit')).toBe(false)
    expect(isProjectScopeCurrent(current, 'canRecycle')).toBe(false)
    slow.resolve(project(2, viewer))
    await Promise.resolve(); await Promise.resolve()
    expect(useProjectStore.getState().project?.access).toEqual(viewer)
    expect(useProjectStore.getState().project?.title).toBe('书2')
  })

  it('刷新看到协作降级/撤销后，旧回调不能应用写响应', async () => {
    const slow = deferred<Project>()
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project(2, editor)).mockReturnValueOnce(slow.promise)
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    const refresh = useProjectStore.getState().refresh()
    slow.resolve(project(2, viewer)); await refresh
    expect(isProjectScopeCurrent(scope, 'canEdit')).toBe(false)
    useProjectStore.getState().setProject({ ...project(2, editor), title: '旧保存' }, scope)
    expect(useProjectStore.getState().project).toMatchObject({ title: '书2', access: viewer })
    vi.mocked(projectApi.get).mockRejectedValueOnce(new ApiError(404, '已撤销'))
    await useProjectStore.getState().refresh()
    expect(useProjectStore.getState().project).toBeNull()
  })

  it('相同授权刷新保留作用域，更换授权轮次时拒绝旧轮次响应', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project(2, editor)).mockResolvedValueOnce(project(2, editor))
      .mockResolvedValueOnce(project(2, { ...editor, authorization_version: 'grant-2' }))
    await useProjectStore.getState().load(2)
    const oldScope = captureProjectScope(2)
    await useProjectStore.getState().refresh()
    expect(isProjectScopeCurrent(oldScope, 'canEdit')).toBe(true)
    await useProjectStore.getState().refresh()
    expect(isProjectScopeCurrent(oldScope)).toBe(false)
    expect(isProjectScopeCurrent(captureProjectScope(2), 'canEdit')).toBe(true)
    useProjectStore.getState().setProject({ ...project(2, editor), title: '旧轮次响应' }, oldScope)
    expect(useProjectStore.getState().project?.title).toBe('书2')
  })

  it('旧刷新迟到不能覆盖新的权限快照，降级后恢复也不能复活旧 scope', async () => {
    const slow = deferred<Project>()
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project(2, editor)).mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(project(2, viewer)).mockResolvedValueOnce(project(2, editor))
    await useProjectStore.getState().load(2)
    const oldScope = captureProjectScope(2)
    const oldRefresh = useProjectStore.getState().refresh()
    await useProjectStore.getState().refresh()
    expect(useProjectStore.getState().project?.access).toEqual(viewer)
    slow.resolve(project(2, editor)); await oldRefresh
    expect(useProjectStore.getState().project?.access).toEqual(viewer)
    await useProjectStore.getState().refresh()
    expect(isProjectScopeCurrent(oldScope)).toBe(false)
    expect(isProjectScopeCurrent(captureProjectScope(2), 'canEdit')).toBe(true)
  })

  it('同项目较早的重新加载不能覆盖其后完成的刷新', async () => {
    const slow = deferred<Project>()
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project(2, editor)).mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(project(2, viewer))
    await useProjectStore.getState().load(2)
    const reload = useProjectStore.getState().load(2)
    await useProjectStore.getState().refresh()
    slow.resolve(project(2, editor)); await reload
    expect(useProjectStore.getState().project?.access).toEqual(viewer)
  })

  it('会话代次失效立即终止作用域，即使旧 user 状态仍暂时存在', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project(1))
    await useProjectStore.getState().load(1)
    const scope = captureProjectScope(1)
    invalidateRequestGeneration()
    expect(isProjectScopeCurrent(scope)).toBe(false)
    expect(captureProjectScope(1)).toBeNull()
    useProjectStore.getState().setProject({ ...project(1), title: '迟到的响应' }, scope)
    expect(useProjectStore.getState().project?.title).toBe('书1')
  })

  it('临时刷新失败保留项目与内容；新作用域保存可生效', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project(1)).mockRejectedValueOnce(new ApiError(0, '网络失败'))
    await useProjectStore.getState().load(1)
    const scope = captureProjectScope(1)
    await useProjectStore.getState().refresh()
    expect(isProjectScopeCurrent(scope, 'canEdit')).toBe(true)
    useProjectStore.getState().setProject({ ...project(1), title: '缺少作用域' }, null)
    expect(useProjectStore.getState().project?.title).toBe('书1')
    useProjectStore.getState().setProject({ ...project(1), title: '已保存' }, scope)
    expect(useProjectStore.getState().project?.title).toBe('已保存')
  })

  it('不复用旧项目迟到失败；当前 403/404 清除对应项目', async () => {
    const slow = deferred<Project>()
    vi.spyOn(projectApi, 'get').mockReturnValueOnce(slow.promise).mockResolvedValueOnce(project(2, editor))
      .mockRejectedValueOnce(new ApiError(403, '无权'))
    const first = useProjectStore.getState().load(1)
    await useProjectStore.getState().load(2)
    slow.reject(new ApiError(404, '旧项目不存在')); await first
    expect(useProjectStore.getState().project?.id).toBe(2)
    await useProjectStore.getState().refresh()
    expect(useProjectStore.getState().project).toBeNull()
  })
})
