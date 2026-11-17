import { create } from 'zustand'
import { ApiError, currentRequestGeneration, setProjectFailureCapture, watchRequestGeneration, type RequestOptions } from '../api/client'
import { projectApi } from '../api/projects'
import type { Project, ProjectAccess, User } from '../api/types'
import { useAuthStore } from './auth'
import { authorizationKey, can } from '../utils/permissions'
import { projectAccess } from '../utils/projectAccess'

/** 请求开始时捕获，写响应落地之前必须仍属于同一登录、作品与能力代次。 */
export interface ProjectScope {
  readonly projectId: number
  readonly actorId: number
  readonly identity: string
  readonly version: number
}

export type ProjectCapability = 'canRead' | 'canEdit' | 'canRecycle' | 'canExport' | 'canUseAI'
  | 'canUseAIEntitled' | 'canUseOwnerAI' | 'canUseAgent' | 'canViewAgent' | 'canManageCollaborators' | 'canBackup'
  | 'canWriteReadingProgress'
export type ProjectAccessCheck = 'confirmed' | 'denied' | 'unknown' | 'stale'

function authIdentity(): string | null {
  const auth = useAuthStore.getState()
  if (auth.status !== 'authenticated' || !auth.user) return null
  return `${auth.user.id}:${authorizationKey(auth.user)}:${currentRequestGeneration()}`
}

function accessible(project: Project, user: User | null, projectId: number): boolean {
  const access = projectAccess(project, user, projectId)
  return access.ready && access.canRead
}

/** RBAC 更新时，只保留快照中有独立来源的已有能力，新增能力等待服务器刷新。 */
function restrictForCurrentActor(project: Project, user: User): Project {
  const old = project.access
  const owner = old.role === 'owner' && (!project.owner || project.owner.id === user.id)
  const collaborator = old.is_collaborator
  const adminRead = can(user, 'projects.read')
  const adminEdit = adminRead && can(user, 'projects.edit')
  const access: ProjectAccess = {
    ...old,
    can_read: old.can_read && (owner || collaborator || adminRead),
    // 协作者的 AI 能力是服务端确认的 editor 来源；管理员本身不能获得该能力。
    can_edit_content: old.can_edit_content && (owner || (collaborator && old.can_use_ai) || adminEdit),
    can_export: old.can_export && (owner || collaborator),
    can_use_ai: old.can_use_ai && (owner || collaborator),
    can_manage_collaborators: old.can_manage_collaborators && owner,
    can_recycle: old.can_recycle && (owner || (adminRead && can(user, 'projects.recycle'))),
    can_backup: old.can_backup && owner,
    can_write_reading_progress: old.can_write_reading_progress && owner,
  }
  return { ...project, access }
}

function sameAccess(first: ProjectAccess, second: ProjectAccess): boolean {
  return first.role === second.role && first.is_collaborator === second.is_collaborator
    && first.authorization_version === second.authorization_version
    && first.allow_owner_ai === second.allow_owner_ai && first.can_read === second.can_read
    && first.can_edit_content === second.can_edit_content && first.can_export === second.can_export
    && first.can_use_ai === second.can_use_ai && first.can_manage_collaborators === second.can_manage_collaborators
    && first.can_recycle === second.can_recycle && first.can_backup === second.can_backup
    && first.can_write_reading_progress === second.can_write_reading_progress
}

interface ProjectState {
  project: Project | null
  loading: boolean
  error: string | null
  scopeVersion: number
  load: (id: number, options?: Pick<RequestOptions, 'signal' | 'isCurrent'>) => Promise<void>
  /** 临时网络失败保留当前项目；明确失权时撤下项目。 */
  refresh: () => Promise<void>
  /** 与 refresh 使用同一权威读取，供冻结的草稿区分暂时失败与已确认权限。 */
  verifyAccess: () => Promise<ProjectAccessCheck>
  /** 只接受发起请求时捕获且仍有效的作用域。 */
  setProject: (p: Project, scope: ProjectScope | null) => void
  discard: (id: number) => void
  reset: () => void
}

let requestSeq = 0
let refreshSeq = 0
let targetId: number | null = null
let boundIdentity: string | null = null
let scopeVersion = 0

function nextScope(): number { scopeVersion += 1; return scopeVersion }

export const useProjectStore = create<ProjectState>((set, get) => ({
  project: null,
  loading: false,
  error: null,
  scopeVersion,
  load: async (id, options) => {
    const requestCurrent = () => !options?.signal?.aborted && (options?.isCurrent?.() ?? true)
    if (!requestCurrent()) return
    const identity = authIdentity()
    const actor = useAuthStore.getState().user
    const seq = ++requestSeq
    const revision = ++refreshSeq
    targetId = id
    const same = get().project?.id === id && boundIdentity === identity
    if (!same) boundIdentity = null
    set({ loading: true, error: null, project: same ? get().project : null, scopeVersion: nextScope() })
    try {
      const project = await (options ? projectApi.get(id, options) : projectApi.get(id))
      if (seq !== requestSeq || revision !== refreshSeq || identity === null || authIdentity() !== identity
        || targetId !== id || !requestCurrent()) return
      if (!accessible(project, actor, id)) {
        boundIdentity = null
        set({ project: null, error: '没有读取此作品的权限', scopeVersion: nextScope() })
        return
      }
      const previous = get().project
      boundIdentity = identity
      set({ project, scopeVersion: !previous || !sameAccess(previous.access, project.access) ? nextScope() : scopeVersion })
    } catch (err) {
      if (seq !== requestSeq || revision !== refreshSeq || authIdentity() !== identity || targetId !== id
        || !requestCurrent()) return
      if (err instanceof ApiError && [403, 404].includes(err.status)) boundIdentity = null
      set({ ...(err instanceof ApiError && [403, 404].includes(err.status)
        ? { project: null, scopeVersion: nextScope() } : {}), error: err instanceof Error ? err.message : '加载项目失败' })
    } finally {
      if (seq === requestSeq) set({ loading: false })
    }
  },
  refresh: async () => { await get().verifyAccess() },
  verifyAccess: async () => {
    const current = get().project
    const identity = authIdentity()
    const actor = useAuthStore.getState().user
    if (!current || identity === null || boundIdentity !== identity || targetId !== current.id) return 'stale'
    const seq = requestSeq
    const refresh = ++refreshSeq
    try {
      const project = await projectApi.get(current.id)
      if (seq !== requestSeq || refresh !== refreshSeq || authIdentity() !== identity
        || get().project?.id !== current.id || targetId !== current.id) return 'stale'
      if (!accessible(project, actor, current.id)) {
        boundIdentity = null
        set({ project: null, error: '没有读取此作品的权限', scopeVersion: nextScope() })
        return 'denied'
      }
      const previous = get().project
      // 同能力的新邀请仍是新授权；普通数据刷新保留草稿及其作用域。
      set({ project, scopeVersion: previous && sameAccess(previous.access, project.access) ? scopeVersion : nextScope() })
      return 'confirmed'
    } catch (err) {
      if (err instanceof ApiError && [403, 404].includes(err.status) && seq === requestSeq && refresh === refreshSeq
        && authIdentity() === identity && get().project?.id === current.id) {
        ++requestSeq
        boundIdentity = null
        targetId = null
        set({ project: null, loading: false, error: err.message, scopeVersion: nextScope() })
        return 'denied'
      }
      return seq === requestSeq && refresh === refreshSeq && authIdentity() === identity && targetId === current.id
        ? 'unknown' : 'stale'
    }
  },
  setProject: (project, scope) => {
    const current = get().project
    if (!current || targetId !== project.id || current.id !== project.id || boundIdentity !== authIdentity()
      || !accessible(project, useAuthStore.getState().user, project.id) || !isProjectScopeCurrent(scope)) return
    ++requestSeq
    ++refreshSeq
    set({ project, loading: false, scopeVersion: sameAccess(current.access, project.access) ? scopeVersion : nextScope() })
  },
  discard: (id) => {
    if (targetId !== id && get().project?.id !== id) return
    ++requestSeq
    ++refreshSeq
    targetId = null
    boundIdentity = null
    set({ project: null, loading: false, error: null, scopeVersion: nextScope() })
  },
  reset: () => {
    ++requestSeq
    ++refreshSeq
    targetId = null
    boundIdentity = null
    set({ project: null, loading: false, error: null, scopeVersion: nextScope() })
  },
}))

export function captureProjectScope(projectId: number): ProjectScope | null {
  const state = useProjectStore.getState()
  const auth = useAuthStore.getState()
  const identity = authIdentity()
  if (!identity || boundIdentity !== identity || !auth.user || targetId !== projectId || !state.project
    || !accessible(state.project, auth.user, projectId)) return null
  return { projectId, actorId: auth.user.id, identity, version: state.scopeVersion }
}

export function isProjectScopeCurrent(scope: ProjectScope | null, capability: ProjectCapability = 'canRead'): boolean {
  if (!scope) return false
  const state = useProjectStore.getState()
  const auth = useAuthStore.getState()
  return state.scopeVersion === scope.version && targetId === scope.projectId && boundIdentity === scope.identity
    && authIdentity() === scope.identity && auth.user?.id === scope.actorId
    && projectAccess(state.project, auth.user, scope.projectId)[capability]
}

/** 只接受原作用域中的权威结果；并发刷新抢先时重试一次读事实，绝不重放业务写入。 */
export async function verifyProjectScope(scope: ProjectScope | null, afterFailure = false): Promise<ProjectAccessCheck> {
  if (!scope || !isProjectScopeCurrent(scope)) return 'stale'
  const pending = failureRefresh && sameScope(failureRefresh.scope, scope) ? failureRefresh
    : afterFailure && recentFailure && sameScope(recentFailure.scope, scope) ? recentFailure : null
  if (pending) {
    const result = await pending.promise
    if (!isProjectScopeCurrent(scope)) return 'stale'
    if (result !== 'stale' || afterFailure) return result
  }
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const result = await useProjectStore.getState().verifyAccess()
    if (!isProjectScopeCurrent(scope)) return 'stale'
    if (result !== 'stale') return result
  }
  return 'unknown'
}

// 同时失败的请求合并刷新；若刷新进行中又收到失败，完成后再读一次，避免错过其后的权限变化。
let failureRefresh: { scope: ProjectScope; dirty: boolean; promise: Promise<ProjectAccessCheck> } | null = null
let recentFailure: { scope: ProjectScope; promise: Promise<ProjectAccessCheck> } | null = null
function sameScope(first: ProjectScope, second: ProjectScope): boolean {
  return first.version === second.version && first.identity === second.identity && first.projectId === second.projectId
}
function refreshAfterFailure(scope: ProjectScope): Promise<ProjectAccessCheck> {
  if (!isProjectScopeCurrent(scope)) return Promise.resolve('stale')
  if (failureRefresh && sameScope(failureRefresh.scope, scope)) {
    failureRefresh.dirty = true
    return failureRefresh.promise
  }
  const pending = { scope, dirty: false, promise: Promise.resolve<ProjectAccessCheck>('stale') }
  failureRefresh = pending
  const refreshing = useProjectStore.getState().verifyAccess()
  const seq = requestSeq
  const revision = refreshSeq
  pending.promise = refreshing.then((result): ProjectAccessCheck | Promise<ProjectAccessCheck> => {
    if (failureRefresh !== pending) return 'stale'
    failureRefresh = null
    // 同作用域写响应或另一刷新可使这次读取被丢弃；仍需确认权限事实。
    if (pending.dirty || requestSeq !== seq || refreshSeq !== revision) return refreshAfterFailure(scope)
    return result
  })
  recentFailure = pending
  return pending.promise
}

setProjectFailureCapture((target) => {
  let pathname: string | null = null
  let projectId: number
  if ('projectId' in target) {
    projectId = target.projectId
  } else {
    // 旧章节接口不带作品 ID；仅从明确指向同一章节的项目页面绑定，离开页面后不再应用。
    pathname = typeof window === 'undefined' ? null : window.location.pathname
    const route = pathname && /^\/projects\/(\d+)\/chapters\/(\d+)(?:\/|$)/.exec(pathname)
    if (!route || Number(route[2]) !== target.chapterId) return null
    projectId = Number(route[1])
  }
  const scope = captureProjectScope(projectId)
  if (!scope) return null
  return {
    isCurrent: () => (pathname === null || (typeof window !== 'undefined' && window.location.pathname === pathname))
      && isProjectScopeCurrent(scope),
    refresh: () => { void refreshAfterFailure(scope) },
  }
})

// 即使 auth store 还未完成异步登出，旧 session 的回调也不能继续应用。
watchRequestGeneration(() => {
  if (targetId === null) return
  ++requestSeq
  ++refreshSeq
  boundIdentity = null
  useProjectStore.setState({ loading: false, scopeVersion: nextScope() })
})

useAuthStore.subscribe((next, previous) => {
  const state = useProjectStore.getState()
  if (next.status !== 'authenticated' || next.user?.id !== previous.user?.id) {
    state.reset()
    return
  }
  if (!next.user || !targetId) return
  const identity = authIdentity()
  if (identity === boundIdentity) return
  const project = state.project
  ++requestSeq
  ++refreshSeq
  boundIdentity = identity
  if (!project || project.id !== targetId) {
    useProjectStore.setState({ loading: false, scopeVersion: nextScope() })
    void state.load(targetId)
    return
  }
  const restricted = restrictForCurrentActor(project, next.user)
  if (!accessible(restricted, next.user, project.id)) {
    state.reset()
    return
  }
  useProjectStore.setState({ project: restricted, loading: false, scopeVersion: nextScope() })
  void useProjectStore.getState().refresh()
})
