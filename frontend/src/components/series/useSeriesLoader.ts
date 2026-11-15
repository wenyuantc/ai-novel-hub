import { useCallback, useEffect, useRef } from 'react'
import { useLoader } from '../../hooks/useLoader'
import type { ProjectResource } from '../../utils/projectRefresh'
import { useSeriesAccessLoss, useSeriesAuthorization } from './seriesAccessContext'
import { currentSeriesIdentity, useSeriesIdentity } from './useSeriesAction'

/** 普通 useLoader 在卸载时丢弃结果；这里还即时校验会话代次及 403/404。 */
export function useSeriesLoader<T>(
  fn: () => Promise<T>, scope?: { projectId: number; resources: readonly ProjectResource[] }, onResult?: (result: T) => void,
) {
  const identity = useSeriesIdentity()
  const accessLoss = useSeriesAccessLoss()
  const authorization = useSeriesAuthorization()
  const serial = useRef(0)
  useEffect(() => () => { serial.current += 1 }, [fn, identity])
  const load = useCallback(async () => {
    const request = ++serial.current
    const ticket = authorization.capture()
    const current = () => identity === currentSeriesIdentity() && request === serial.current && authorization.current(ticket)
    try {
      const result = await fn()
      if (!current()) throw new Error('会话或系列权限已变化，请重新读取')
      onResult?.(result)
      // 结果本身可能带来新的授权轮次；旧加载器不能再接收它。
      if (!current()) throw new Error('会话或系列权限已变化，请重新读取')
      return result
    } catch (error) {
      if (current()) accessLoss(error)
      throw error
    }
  }, [accessLoss, authorization, fn, identity, onResult])
  return useLoader(load, scope)
}
