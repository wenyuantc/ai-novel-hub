import { create } from 'zustand'
import { projectApi } from '../api/projects'
import { ApiError } from '../api/client'
import type { Project } from '../api/types'

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
    const seq = ++requestSeq
    targetId = id
    set({ loading: true, error: null, project: get().project?.id === id ? get().project : null })
    try {
      const project = await projectApi.get(id)
      if (seq === requestSeq) set({ project })
    } catch (err) {
      if (seq === requestSeq) set({ ...(err instanceof ApiError && err.status === 404 ? { project: null } : {}), error: err instanceof Error ? err.message : '加载项目失败' })
    } finally {
      if (seq === requestSeq) set({ loading: false })
    }
  },
  refresh: async () => {
    const current = get().project
    if (!current) return
    const seq = requestSeq
    const refresh = ++refreshSeq
    try {
      const project = await projectApi.get(current.id)
      if (seq === requestSeq && refresh === refreshSeq && get().project?.id === current.id) set({ project })
    } catch (err) {
      if (err instanceof ApiError && err.status === 404 && seq === requestSeq && refresh === refreshSeq && get().project?.id === current.id) {
        ++requestSeq
        targetId = null
        set({ project: null, loading: false, error: err.message })
      }
      // 临时网络失败仍静默；明确 404 则停止挂载已不可访问作品的编辑/任务副作用。
    }
  },
  setProject: (project) => {
    if (targetId !== project.id || get().project?.id !== project.id) return
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
