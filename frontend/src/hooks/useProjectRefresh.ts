import { useEffect, useRef } from 'react'
import { subscribeProjectRefresh, type ProjectResource } from '../utils/projectRefresh'

export function useProjectRefresh(projectId: number | undefined, resources: readonly ProjectResource[], refresh: () => void) {
  const refreshRef = useRef(refresh)
  useEffect(() => { refreshRef.current = refresh }, [refresh])
  const scope = resources.join(',')
  useEffect(() => subscribeProjectRefresh((pid, changed) => {
    if (pid === projectId && changed.some((key) => scope.split(',').includes(key))) refreshRef.current()
  }), [projectId, scope])
}
