import { create } from 'zustand'
import { useEffect } from 'react'
import { errorMessage } from '../api/client'
import { styleApi } from '../api/styles'
import type { WritingStyle } from '../api/types'

interface StyleState {
  styles: WritingStyle[]
  loaded: boolean
  loading: boolean
  error: string | null
  load: (force?: boolean) => Promise<void>
  reset: () => void
}

/** 可见写作风格（预设 + 我的）：项目概览、章节编辑器、批量弹窗共用；风格库页变更后 load(true) */
let generation = 0

export const useStyleStore = create<StyleState>((set, get) => ({
  styles: [],
  loaded: false,
  loading: false,
  error: null,
  load: async (force = false) => {
    if (get().loading || (get().loaded && !force)) return
    const ticket = ++generation
    set({ loading: true })
    try {
      const styles = await styleApi.list()
      if (ticket !== generation) return
      set({ styles, loaded: true, error: null })
    } catch (err) {
      if (ticket === generation) set({ error: errorMessage(err) })
    } finally {
      if (ticket === generation) set({ loading: false })
    }
  },
  reset: () => { generation += 1; set({ styles: [], loaded: false, loading: false, error: null }) },
}))

/** 挂载时加载风格列表（已加载则不重复请求），返回列表与加载状态 */
export function useStyles(): { styles: WritingStyle[]; loading: boolean; error: string | null } {
  const styles = useStyleStore((s) => s.styles)
  const loading = useStyleStore((s) => s.loading)
  const error = useStyleStore((s) => s.error)
  const load = useStyleStore((s) => s.load)
  useEffect(() => {
    void load()
  }, [load])
  return { styles, loading, error }
}
