import { useEffect, useRef, useState } from 'react'
import { currentRequestGeneration, errorMessage, watchRequestGeneration } from '../api/client'
import { penNamesApi } from '../api/penNames'
import type { PenName } from '../api/types'
import { useAuthStore } from '../stores/auth'

/** 快照用于异步写入：登出开始、账号切换或权限修订后不能回填旧 UI。 */
export function capturePenNameActor(): () => boolean {
  const auth = useAuthStore.getState()
  const id = auth.user?.id
  const revision = auth.user?.authorization_revision
  const generation = currentRequestGeneration()
  return () => {
    const current = useAuthStore.getState()
    return id !== undefined && auth.status === 'authenticated' && current.status === 'authenticated'
      && current.user?.id === id && current.user.authorization_revision === revision
      && currentRequestGeneration() === generation
  }
}

export function usePenNameIdentity(): string {
  const id = useAuthStore((s) => s.user?.id)
  const revision = useAuthStore((s) => s.user?.authorization_revision)
  const status = useAuthStore((s) => s.status)
  const [generation, setGeneration] = useState(currentRequestGeneration)
  useEffect(() => {
    const watcher = watchRequestGeneration(() => setGeneration(currentRequestGeneration()))
    return watcher.dispose
  }, [])
  return `${status}:${id ?? ''}:${revision ?? ''}:${generation}`
}

interface State { identity: string; items: PenName[]; loading: boolean; error: string | null }

/** 私库只保存在组件实例内；旧身份的成功和失败响应均不显示。 */
export function usePenNames(enabled = true) {
  const identity = usePenNameIdentity()
  const [version, setVersion] = useState(0)
  const [state, setState] = useState<State>({ identity: '', items: [], loading: true, error: null })
  const sequence = useRef(0)

  useEffect(() => {
    const seq = ++sequence.current
    const currentActor = capturePenNameActor()
    if (!enabled || !currentActor()) return
    const controller = new AbortController()
    const isCurrent = () => seq === sequence.current && !controller.signal.aborted && currentActor()
    void penNamesApi.list({ signal: controller.signal, isCurrent }).then(
      (items) => { if (isCurrent()) setState({ identity, items, loading: false, error: null }) },
      (err: unknown) => { if (isCurrent()) setState({ identity, items: [], loading: false, error: errorMessage(err) }) },
    )
    return () => { sequence.current += 1; controller.abort() }
  }, [enabled, identity, version])

  const visible = enabled && state.identity === identity
    ? state : { items: [], loading: enabled, error: null }
  return { ...visible, reload: () => setVersion((v) => v + 1) }
}
