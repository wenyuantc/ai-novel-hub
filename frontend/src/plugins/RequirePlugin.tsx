import { Alert, Button, Spin } from 'antd'
import { Suspense, useEffect, type ReactNode } from 'react'
import { useAuthStore } from '../stores/auth'

export default function RequirePlugin({ id, children }: { id: string; children: ReactNode }) {
  const ready = useAuthStore((state) => state.pluginConfigReady)
  const enabled = useAuthStore((state) => state.enabledPlugins.includes(id))
  const refresh = useAuthStore((state) => state.refreshConfig)
  useEffect(() => { if (!ready) void refresh() }, [ready, refresh])
  if (!ready) return <Alert message="正在读取功能插件状态" action={<Button onClick={() => void refresh()}>重新读取</Button>} />
  if (!enabled) return <Alert showIcon message="该功能插件未启用，请联系管理员。" />
  return <Suspense fallback={<Spin aria-label="正在加载插件页面" />}>{children}</Suspense>
}
