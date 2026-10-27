import { create } from 'zustand'
import type { ResolvedTheme } from './palette'

export type ThemeMode = 'light' | 'dark' | 'system'

export const THEME_STORAGE_KEY = 'ainovel.theme'
const DARK_QUERY = '(prefers-color-scheme: dark)'

/** localStorage 中的值 → 合法模式；缺失 / 非法时默认跟随系统。 */
export function parseThemeMode(raw: unknown): ThemeMode {
  return raw === 'light' || raw === 'dark' || raw === 'system' ? raw : 'system'
}

/** 实际生效的主题：system 时取系统偏好。 */
export function resolveTheme(mode: ThemeMode, systemDark: boolean): ResolvedTheme {
  if (mode === 'system') return systemDark ? 'dark' : 'light'
  return mode
}

function readStoredMode(): ThemeMode {
  try {
    return parseThemeMode(localStorage.getItem(THEME_STORAGE_KEY))
  } catch {
    return 'system'
  }
}

function darkQuery(): MediaQueryList | null {
  try {
    return typeof window !== 'undefined' && typeof window.matchMedia === 'function' ? window.matchMedia(DARK_QUERY) : null
  } catch {
    return null
  }
}

interface ThemeState {
  mode: ThemeMode
  systemDark: boolean
  setMode: (mode: ThemeMode) => void
  setSystemDark: (dark: boolean) => void
}

/** 主题偏好（设备级，不随登录用户重置）：模式持久化在 localStorage，系统偏好由 watchSystemTheme 实时同步。 */
export const useThemeStore = create<ThemeState>((set) => ({
  mode: readStoredMode(),
  systemDark: darkQuery()?.matches ?? false,
  setMode: (mode) => {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, mode)
    } catch {
      // 不可写时只在本次会话生效
    }
    set({ mode })
  },
  setSystemDark: (systemDark) => set({ systemDark }),
}))

/** 监听系统主题变化（App 挂载时调用，返回取消函数）。 */
export function watchSystemTheme(): () => void {
  const mq = darkQuery()
  if (!mq) return () => {}
  const onChange = (e: MediaQueryListEvent) => useThemeStore.getState().setSystemDark(e.matches)
  useThemeStore.getState().setSystemDark(mq.matches)
  mq.addEventListener('change', onChange)
  return () => mq.removeEventListener('change', onChange)
}

export function useResolvedTheme(): ResolvedTheme {
  return useThemeStore((s) => resolveTheme(s.mode, s.systemDark))
}
