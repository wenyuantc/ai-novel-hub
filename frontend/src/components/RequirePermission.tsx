import { useEffect, type ReactNode } from 'react'
import { Alert } from 'antd'
import { Navigate, useLocation } from 'react-router-dom'
import type { PermissionCode } from '../api/types'
import { useAuthStore } from '../stores/auth'
import { can, isSuperAdmin } from '../utils/permissions'
import { FullPageSpin } from './RequireAuth'

export default function RequirePermission({ permission, superOnly, children }: { permission?: PermissionCode; superOnly?: boolean; children: ReactNode }) {
  const status = useAuthStore((s) => s.status)
  const user = useAuthStore((s) => s.user)
  const location = useLocation()
  useEffect(() => { if (status === 'authenticated') void useAuthStore.getState().refreshAuthorization() }, [status, user?.id, permission, superOnly, location.pathname])
  if (status === 'unknown') return <FullPageSpin />
  if (status !== 'authenticated' || !user) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  if (superOnly ? !isSuperAdmin(user) : !permission || !can(user, permission)) return <Alert type="warning" showIcon message="没有访问此管理功能的权限" />
  return <>{children}</>
}
