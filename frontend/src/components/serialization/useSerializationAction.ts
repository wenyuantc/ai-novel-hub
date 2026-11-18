import { useLayoutEffect, useRef, useState } from 'react'
import { currentRequestGeneration, errorMessage } from '../../api/client'
import { useAuthStore } from '../../stores/auth'
import type { EditorActorIdentity } from '../../api/client'

interface ActionOptions { signal: AbortSignal; isCurrent: () => boolean; editorActor?: EditorActorIdentity }

/** 以弹层、作品权限和登录代次约束一次写入或下载。 */
export function useSerializationAction(canAccess: () => boolean) {
  const pending = useRef<AbortController | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useLayoutEffect(() => () => { pending.current?.abort() }, [canAccess])
  async function run<T>(request: (options: ActionOptions) => Promise<T>, success: (result: T) => void, onConflict?: () => void) {
    if (pending.current || !canAccess()) return
    const controller = new AbortController()
    pending.current = controller
    const generation = currentRequestGeneration()
    const actor = useAuthStore.getState().user
    const options = { signal: controller.signal, isCurrent: () => !controller.signal.aborted && canAccess()
      && generation === currentRequestGeneration(), editorActor: actor ? { id: actor.id, createdAt: actor.created_at } : undefined }
    setBusy(true)
    setError('')
    try {
      const result = await request(options)
      if (options.isCurrent()) success(result)
    } catch (err) {
      if (options.isCurrent()) {
        setError(errorMessage(err))
        if (err instanceof Error && 'status' in err && err.status === 409) onConflict?.()
      }
    } finally {
      if (pending.current === controller) {
        pending.current = null
        if (options.isCurrent()) setBusy(false)
      }
    }
  }
  return { run, busy, error, clearError: () => setError('') }
}
