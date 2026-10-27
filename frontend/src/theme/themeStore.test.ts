import { afterEach, describe, expect, it, vi } from 'vitest'
import { parseThemeMode, resolveTheme, useThemeStore, watchSystemTheme } from './themeStore'

describe('主题解析', () => {
  it('非法 / 缺失的存储值默认跟随系统', () => {
    expect(parseThemeMode('dark')).toBe('dark')
    expect(parseThemeMode('light')).toBe('light')
    expect(parseThemeMode('system')).toBe('system')
    expect(parseThemeMode(null)).toBe('system')
    expect(parseThemeMode('blue')).toBe('system')
  })

  it('system 模式按系统偏好解析，其余模式固定', () => {
    expect(resolveTheme('system', true)).toBe('dark')
    expect(resolveTheme('system', false)).toBe('light')
    expect(resolveTheme('light', true)).toBe('light')
    expect(resolveTheme('dark', false)).toBe('dark')
  })
})

describe('系统主题监听', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('matchMedia 变化实时写入 store，取消后不再响应', () => {
    let listener: ((e: { matches: boolean }) => void) | null = null
    const mq = {
      matches: false,
      addEventListener: (_: string, fn: (e: { matches: boolean }) => void) => {
        listener = fn
      },
      removeEventListener: () => {
        listener = null
      },
    }
    vi.stubGlobal('window', { matchMedia: () => mq })
    const stop = watchSystemTheme()
    expect(useThemeStore.getState().systemDark).toBe(false)
    listener!({ matches: true })
    expect(useThemeStore.getState().systemDark).toBe(true)
    expect(resolveTheme('system', useThemeStore.getState().systemDark)).toBe('dark')
    stop()
    expect(listener).toBeNull()
  })

  it('setMode 写入 localStorage', () => {
    const store: Record<string, string> = {}
    vi.stubGlobal('localStorage', { setItem: (k: string, v: string) => (store[k] = v), getItem: (k: string) => store[k] ?? null })
    useThemeStore.getState().setMode('dark')
    expect(store['ainovel.theme']).toBe('dark')
    expect(useThemeStore.getState().mode).toBe('dark')
  })
})
