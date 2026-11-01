import { App as AntApp, ConfigProvider, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { useEffect } from 'react'
import { RouterProvider } from 'react-router-dom'
import { setUnauthorizedHandler, setPermissionDeniedHandler } from './api/client'
import { router } from './router'
import { useAIConfigStore } from './stores/aiConfigs'
import { useAuthStore } from './stores/auth'
import { useProjectStore } from './stores/project'
import { useSkillStore } from './stores/skills'
import { useStyleStore } from './stores/styles'
import { useTasksStore } from './stores/tasks'
import { studioPalette } from './theme/palette'
import ThemeSync from './theme/ThemeSync'
import { useResolvedTheme, watchSystemTheme } from './theme/themeStore'
import { startPublicConfigPolling } from './utils/publicConfig'

export default function App() {
  const init = useAuthStore((s) => s.init)
  const userId = useAuthStore((s) => s.user?.id)
  const resolved = useResolvedTheme()
  const colors = studioPalette(resolved)

  useEffect(() => {
    // 任意接口返回 401：清空会话，RequireAuth 会跳转到登录页
    setUnauthorizedHandler(() => useAuthStore.getState().clear())
    setPermissionDeniedHandler(() => { void useAuthStore.getState().refreshAuthorization() })
    void init()
    return () => { setUnauthorizedHandler(null); setPermissionDeniedHandler(null) }
  }, [init])

  useEffect(() => {
    const stop = startPublicConfigPolling(() => { void useAuthStore.getState().refreshConfig() }, document, window)
    return () => { stop(); useAuthStore.getState().cancelConfig() }
  }, [])

  useEffect(() => {
    if (userId === undefined) return
    const stop = startPublicConfigPolling(() => { void useAuthStore.getState().refreshAuthorization() }, document, window)
    return () => { stop(); useAuthStore.getState().cancelAuthorization() }
  }, [userId])

  // 跟随系统：监听 prefers-color-scheme 变化
  useEffect(() => watchSystemTheme(), [])

  // 在账号变更的同一通知中清空缓存，不能在新页面load之后再由父effect作废它。
  useEffect(() => {
    const reset = () => {
      useAIConfigStore.getState().reset()
      useProjectStore.getState().reset()
      useTasksStore.getState().reset()
      useStyleStore.getState().reset()
      useSkillStore.getState().reset()
    }
    reset()
    let owner = useAuthStore.getState().user?.id
    return useAuthStore.subscribe((auth) => {
      const next = auth.user?.id
      if (next !== owner) { owner = next; reset() }
    })
  }, [])

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: resolved === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          colorPrimary: colors.primary,
          colorInfo: colors.primary,
          colorSuccess: colors.success,
          colorWarning: colors.warning,
          colorError: colors.danger,
          colorBgLayout: colors.surface,
          colorBgContainer: colors['surface-raised'],
          colorBgElevated: colors['surface-raised'],
          colorText: colors['surface-foreground'],
          colorTextSecondary: colors.muted,
          colorTextPlaceholder: colors.muted,
          colorTextLightSolid: colors['primary-foreground'],
          colorBorder: colors.border,
          colorBorderSecondary: colors.border,
          colorLink: colors.primary,
          colorFillAlter: colors.panel,
          fontFamily: '"Geist Variable", "PingFang SC", "Microsoft YaHei", sans-serif',
          fontSize: 14,
          borderRadius: 7,
          borderRadiusLG: 10,
          controlHeight: 36,
        },
        components: {
          Button: { primaryColor: colors['primary-foreground'], primaryShadow: 'none', defaultShadow: 'none' },
          Menu: { itemBg: 'transparent', itemSelectedBg: colors.active, itemSelectedColor: colors['surface-foreground'] },
        },
      }}
    >
      <ThemeSync resolved={resolved} />
      <AntApp>
        <RouterProvider router={router} />
      </AntApp>
    </ConfigProvider>
  )
}
