import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { useEffect } from 'react'
import { RouterProvider } from 'react-router-dom'
import { setUnauthorizedHandler } from './api/client'
import { router } from './router'
import { useAIConfigStore } from './stores/aiConfigs'
import { useAuthStore } from './stores/auth'
import { useProjectStore } from './stores/project'
import { useStyleStore } from './stores/styles'
import { useTasksStore } from './stores/tasks'

export default function App() {
  const init = useAuthStore((s) => s.init)
  const userId = useAuthStore((s) => s.user?.id)

  useEffect(() => {
    // 任意接口返回 401：清空会话，RequireAuth 会跳转到登录页
    setUnauthorizedHandler(() => useAuthStore.getState().clear())
    void init()
    return () => setUnauthorizedHandler(null)
  }, [init])

  // 切换/退出账户时清空与用户相关的缓存
  useEffect(() => {
    useAIConfigStore.getState().reset()
    useProjectStore.getState().reset()
    useTasksStore.getState().reset()
    useStyleStore.getState().reset()
  }, [userId])

  return (
    <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#1677ff' } }}>
      <AntApp>
        <RouterProvider router={router} />
      </AntApp>
    </ConfigProvider>
  )
}
