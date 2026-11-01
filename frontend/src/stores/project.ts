import { create } from 'zustand'
import { projectApi } from '../api/projects'
import { ApiError } from '../api/client'
import type { Project } from '../api/types'
import { useAuthStore } from './auth'
import { can } from '../utils/permissions'

function accessible(project: Project, actorId: number | undefined) {
  const auth = useAuthStore.getState()
  return auth.status === 'authenticated' && auth.user?.id === actorId && (!project.owner || project.owner.id === actorId || can(auth.user, 'projects.read'))
}

interface ProjectState {
  project: Project | null
  loading: boolean
  error: string | null
  load: (id: number) => Promise<void>
  /** 静默刷新（例如章节字数变化后） */
  refresh: () => Promise<void>
  setProject: (p: Project) => void
  /** 删除/回收站操作只作废目标项目，不影响其他作品的未保存内容。 */
  discard: (id: number) => void
  reset: () => void
}

let requestSeq = 0
let refreshSeq = 0
let targetId: number | null = null

export const useProjectStore = create<ProjectState>((set, get) => ({
  project: null,
  loading: false,
  error: null,
  load: async (id) => {
    const actorId = useAuthStore.getState().user?.id
    const seq = ++requestSeq
    targetId = id
    set({ loading: true, error: null, project: get().project?.id === id ? get().project : null })
    try {
      const project = await projectApi.get(id)
      if (seq === requestSeq) set(accessible(project, actorId) ? { project } : { project: null, error: '没有读取此作品的权限' })
    } catch (err) {
      if (seq === requestSeq) set({ ...(err instanceof ApiError && [403, 404].includes(err.status) ? { project: null } : {}), error: err instanceof Error ? err.message : '加载项目失败' })
    } finally {
      if (seq === requestSeq) set({ loading: false })
    }
  },
  refresh: async () => {
    const actorId = useAuthStore.getState().user?.id
    const current = get().project
    if (!current) return
    const seq = requestSeq
    const refresh = ++refreshSeq
    try {
      const project = await projectApi.get(current.id)
      if (seq === requestSeq && refresh === refreshSeq && get().project?.id === current.id && accessible(project, actorId)) set({ project })
    } catch (err) {
      if (err instanceof ApiError && [403, 404].includes(err.status) && seq === requestSeq && refresh === refreshSeq && get().project?.id === current.id) {
        ++requestSeq
        targetId = null
        set({ project: null, loading: false, error: err.message })
      }
      // 临时网络失败仍静默；明确 404 则停止挂载已不可访问作品的编辑/任务副作用。
    }
  },
  setProject: (project) => {
    if (targetId !== project.id || get().project?.id !== project.id || !accessible(project, useAuthStore.getState().user?.id)) return
    ++requestSeq
    targetId = project.id
    set({ project, loading: false })
  },
  discard: (id) => {
    if (targetId !== id && get().project?.id !== id) return
    ++requestSeq
    ++refreshSeq
    targetId = null
    set({ project: null, loading: false, error: null })
  },
  reset: () => {
    ++requestSeq
    targetId = null
    set({ project: null, loading: false, error: null })
  },
}))

// 同 actor 丢失跨用户读取能力时立即清空内容；仍有读取权限的撤写不清只读视图。
useAuthStore.subscribe((next, previous) => {
  const project = useProjectStore.getState().project
  if (next.user?.id !== previous.user?.id || (project && !accessible(project, next.user?.id))) useProjectStore.getState().reset()
})
