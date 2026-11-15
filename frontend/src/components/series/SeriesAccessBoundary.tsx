import { Alert, Button } from 'antd'
import { Fragment, type ReactNode, useCallback, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { SeriesAccessContext, SeriesAuthorizationContext, isSeriesAccessError } from './seriesAccessContext'
import { SeriesAuthorizationScope } from './seriesAuthorization'
import { currentSeriesIdentity, useSeriesIdentity } from './useSeriesAction'

/** 访问失效时立即卸载系列数据与草稿；重试从服务端重新读取。 */
export default function SeriesAccessBoundary({ children, onAccessLost, authorizationVersion }: {
  children: ReactNode; onAccessLost?: () => void; authorizationVersion?: string | null
}) {
  const identity = useSeriesIdentity()
  return <Boundary key={identity} identity={identity} onAccessLost={onAccessLost}
    authorizationVersion={authorizationVersion}>{children}</Boundary>
}

function Boundary({ identity, children, onAccessLost, authorizationVersion }: {
  identity: string; children: ReactNode; onAccessLost?: () => void; authorizationVersion?: string | null
}) {
  const [lost, setLost] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [authorization] = useState(() => new SeriesAuthorizationScope(authorizationVersion))
  const authorizationEpoch = useSyncExternalStore(authorization.subscribe, authorization.capture, authorization.capture)
  const mounted = useRef(false)
  useLayoutEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  useLayoutEffect(() => {
    if (authorizationVersion !== undefined) authorization.adopt(authorizationVersion)
  }, [authorization, authorizationVersion])
  const invalidate = useCallback((error: unknown) => {
    if (mounted.current && identity === currentSeriesIdentity() && isSeriesAccessError(error)) {
      setLost(true)
      onAccessLost?.()
    }
  }, [identity, onAccessLost])
  return <SeriesAccessContext.Provider value={invalidate}><SeriesAuthorizationContext.Provider value={authorization}>
      {lost ? <Alert type="warning" showIcon message="系列访问权限可能已变化，当前内容已清除。"
        action={<Button size="small" onClick={() => { setAttempt((value) => value + 1); setLost(false) }}>重新读取</Button>} />
        : <Fragment key={`${attempt}:${authorizationEpoch}`}>{children}</Fragment>}
  </SeriesAuthorizationContext.Provider></SeriesAccessContext.Provider>
}
