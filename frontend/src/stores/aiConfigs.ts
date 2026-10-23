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

export const useAIConfigStore = create<AIConfigState>((set, get) => ({
  configs: [],
  loaded: false,
  loading: false,
  load: async (force = false) => {
    if (get().loading || (get().loaded && !force)) return
    set({ loading: true })
    try {
      const configs = await aiConfigApi.list()
      set({ configs, loaded: true })
    } finally {
      set({ loading: false })
    }
  },
  reset: () => set({ configs: [], loaded: false }),
}))
