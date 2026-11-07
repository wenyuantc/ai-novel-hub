import { useLayoutEffect, useRef, useState } from 'react'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'

export function useSeriesIdentity() {
  return useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
}

function currentIdentity() {
  const state = useAuthStore.getState()
  return `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`
}

/** 启动后换页、换书或换会话时丢弃迟到结果；不对写请求进行猜测重放。 */
export function useSeriesAction() {
  const identity = useSeriesIdentity()
  const gate = useRef({ mounted: false, serial: 0, pending: false })
  const [state, setState] = useState<{ identity: string; busy: boolean; error: unknown }>({ identity, busy: false, error: null })
  useLayoutEffect(() => {
    const ref = gate.current
    ref.mounted = true
    ref.pending = false
    return () => { ref.mounted = false; ref.serial += 1 }
  }, [identity])

  const run = async <T,>(operation: (current: () => boolean) => Promise<T>, onSuccess: (value: T) => void) => {
    const ref = gate.current
    if (ref.pending || !ref.mounted || identity !== currentIdentity()) return
    const serial = ++ref.serial
    const current = () => ref.mounted && serial === ref.serial && identity === currentIdentity()
    ref.pending = true
    setState({ identity, busy: true, error: null })
    try {
      const value = await operation(current)
      if (current()) onSuccess(value)
    } catch (err) {
      if (current()) setState({ identity, busy: true, error: err })
    } finally {
      if (current()) { ref.pending = false; setState((prev) => ({ ...prev, busy: false })) }
    }
  }
  return {
    busy: state.identity === identity && state.busy,
    error: state.identity === identity ? state.error : null,
    run,
    clearError: () => setState((prev) => ({ ...prev, error: null })),
  }
}
