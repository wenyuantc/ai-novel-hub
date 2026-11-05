import { App } from 'antd'
import { useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { resolveEntityDeepLink } from '../utils/mentions'

/** 只消费自己的 id 参数，沿页面现有打开流程保留版本与草稿守卫。 */
export function useEntityDeepLink<T extends { id: number; project_id: number }>(projectId: number, items: T[] | undefined, loading: boolean, onOpen: (item: T) => void, canReadNow: () => boolean) {
  const [params, setParams] = useSearchParams()
  const openRef = useRef(onOpen)
  const { message } = App.useApp()
  useEffect(() => { openRef.current = onOpen }, [onOpen])
  useEffect(() => {
    if (!canReadNow()) return
    const target = resolveEntityDeepLink(projectId, items, loading, params.get('id'))
    if (target.status === 'waiting') return
    if (target.status === 'found') openRef.current(target.item)
    else message.warning('条目不存在或已被删除')
    setParams((previous) => { const next = new URLSearchParams(previous); next.delete('id'); return next }, { replace: true })
  }, [projectId, items, loading, params, setParams, message, canReadNow])
}
