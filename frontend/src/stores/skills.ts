import { create } from 'zustand'
import { useEffect } from 'react'
import { errorMessage } from '../api/client'
import type { SkillBrief } from '../api/types'

interface SkillState {
  skills: SkillBrief[]
  loaded: boolean
  loading: boolean
  error: string | null
  load: (force?: boolean) => Promise<void>
  reset: () => void
}

/** 可见 Skill（我的 + 全局）：章节编辑器、批量弹窗、智能体面板共用；Skill 页变更后 load(true) */
let generation = 0

export const useSkillStore = create<SkillState>((set, get) => ({
  skills: [],
  loaded: false,
  loading: false,
  error: null,
  load: async (force = false) => {
    if (get().loading || (get().loaded && !force)) return
    const ticket = ++generation
    set({ loading: true })
    try {
      // 按需加载接口模块：store 因 App 的重置 effect 位于入口 chunk，接口与常量不必随入口加载
      const { skillApi } = await import('../api/skills')
      if (ticket !== generation) return
      const skills = await skillApi.list('all')
      if (ticket !== generation) return
      set({ skills, loaded: true, error: null })
    } catch (err) {
      if (ticket === generation) set({ error: errorMessage(err) })
    } finally {
      if (ticket === generation) set({ loading: false })
    }
  },
  reset: () => { generation += 1; set({ skills: [], loaded: false, loading: false, error: null }) },
}))

/** 挂载时加载 Skill 列表（已加载则不重复请求） */
export function useSkills(): { skills: SkillBrief[]; loading: boolean; error: string | null } {
  const skills = useSkillStore((s) => s.skills)
  const loading = useSkillStore((s) => s.loading)
  const error = useSkillStore((s) => s.error)
  const load = useSkillStore((s) => s.load)
  useEffect(() => {
    void load()
  }, [load])
  return { skills, loading, error }
}
