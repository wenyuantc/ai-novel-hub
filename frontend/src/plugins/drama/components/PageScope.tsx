import { Alert, Button, Spin } from 'antd'
import type { ReactNode } from 'react'
import { useParams } from 'react-router-dom'
import { useProjectAccess } from '../../../hooks/useProjectAccess'
import { useAuthStore } from '../../../stores/auth'
import { useProjectStore } from '../../../stores/project'

export type Access = ReturnType<typeof useProjectAccess>
type Content = (pid: number, sid: number, eid: number, access: Access) => ReactNode
export default function PageScope({ children }: { children: Content }) {
  const params = useParams()
  const pid = Number(params.pid)
  const actor = useAuthStore((s) => s.user?.id)
  const scope = useProjectStore((s) => s.scopeVersion)
  return (
    <ScopedContent
      key={[pid, params.sid, params.eid, actor, scope].join('/')}
      pid={pid}
      sid={Number(params.sid)}
      eid={Number(params.eid)}
    >
      {children}
    </ScopedContent>
  )
}

function ScopedContent({ pid, sid, eid, children }: { pid: number; sid: number; eid: number; children: Content }) {
  const access = useProjectAccess(pid)
  if (!access.ready) return <Spin aria-label="正在读取作品权限" />
  if (!access.canRead) return <Alert message="无法读取这部作品" type="warning" />
  return <div className="drama-page">{children(pid, sid, eid, access)}</div>
}

export function LoadError({ error, reload }: { error: string; reload: () => void }) {
  return <Alert type="error" showIcon message={error} action={<Button onClick={reload}>重新加载</Button>} />
}
