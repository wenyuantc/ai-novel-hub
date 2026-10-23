import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { useAuthStore } from '../stores/auth'
import { adminGuard } from '../utils/adminUsers'
import { FullPageSpin } from './RequireAuth'

/** 路由守卫：仅管理员可访问，非管理员重定向首页（接口层仍由后端 403 兜底）。 */
export default function RequireAdmin({ children }: { children: ReactNode }) {
  const status = useAuthStore((s) => s.status)
  const user = useAuthStore((s) => s.user)
  const location = useLocation()
  switch (adminGuard(status, user)) {
    case 'loading':
      return <FullPageSpin />
    case 'login':
      return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
    case 'home':
      return <Navigate to="/" replace />
    case 'allow':
      return <>{children}</>
  }
}
