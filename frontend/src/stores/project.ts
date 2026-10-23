import { create } from 'zustand'
import { projectApi } from '../api/projects'
import type { Project } from '../api/types'

interface ProjectState {
  project: Project | null
  loading: boolean
  error: string | null
  load: (id: number) => Promise<void>
  /** 静默刷新（例如章节字数变化后） */
  refresh: () => Promise<void>
  setProject: (p: Project) => void
  reset: () => void
}

export const useProjectStore = create<ProjectState>((set, get) => ({
  project: null,
  loading: false,
  error: null,
  load: async (id) => {
    set({ loading: true, error: null, project: get().project?.id === id ? get().project : null })
    try {
      const project = await projectApi.get(id)
      set({ project })
    } catch (err) {
      set({ error: err instanceof Error ? err.message : '加载项目失败' })
    } finally {
      set({ loading: false })
    }
  },
  refresh: async () => {
    const current = get().project
    if (!current) return
    try {
      set({ project: await projectApi.get(current.id) })
    } catch {
      // 静默失败
    }
  },
  setProject: (project) => set({ project }),
  reset: () => set({ project: null, error: null }),
}))
