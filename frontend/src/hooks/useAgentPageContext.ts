import { useEffect, useSyncExternalStore } from 'react'
import type { AgentPageContext } from '../api/types'

type Context = { projectId: number; value: Omit<AgentPageContext, 'route'> }
let current: Context | null = null
const listeners = new Set<() => void>()
const subscribe = (fn: () => void) => { listeners.add(fn); return () => { listeners.delete(fn) } }
const snapshot = () => current

/** 选区仅保留在当前页面内存中，卸载即清除；发送时才附带到本轮请求。 */
export function usePublishAgentContext(projectId: number, value: Omit<AgentPageContext, 'route'>) {
  const { chapter_id, outline_id, character_id, selection } = value
  useEffect(() => {
    const next: Context = { projectId, value: { chapter_id, outline_id, character_id, selection: selection ? Array.from(selection).slice(0, 2000).join('') : undefined } }
    current = next
    listeners.forEach((fn) => fn())
    return () => {
      if (current === next) { current = null; listeners.forEach((fn) => fn()) }
    }
  }, [projectId, chapter_id, outline_id, character_id, selection])
}
export function useAgentPageContext(projectId: number, route: string): AgentPageContext {
  const context = useSyncExternalStore(subscribe, snapshot, () => null)
  const match = route.match(/^\/projects\/(\d+)\/chapters\/(\d+)$/)
  return { route, ...(match && Number(match[1]) === projectId ? { chapter_id: Number(match[2]) } : {}), ...(context?.projectId === projectId ? context.value : {}) }
}
