import { create } from 'zustand'
import { aiConfigApi } from '../api/aiConfigs'
import type { AIConfig } from '../api/types'

interface AIConfigState {
  configs: AIConfig[]
  loaded: boolean
  loading: boolean
  load: (force?: boolean) => Promise<void>
  reset: () => void
}

/** 文本生成（向导、章节）只能使用文本模型 */
export const selectTextConfigs = (s: AIConfigState) => s.configs.filter((c) => c.model_type === 'text')

let generation = 0

export const useAIConfigStore = create<AIConfigState>((set, get) => ({
  configs: [],
  loaded: false,
  loading: false,
  load: async (force = false) => {
    if (get().loading || (get().loaded && !force)) return
    const ticket = ++generation
    set({ loading: true })
    try {
      const configs = await aiConfigApi.list()
      if (ticket !== generation) return
      set({ configs, loaded: true })
    } finally {
      if (ticket === generation) set({ loading: false })
    }
  },
  reset: () => { generation += 1; set({ configs: [], loaded: false, loading: false }) },
}))
