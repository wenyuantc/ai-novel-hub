import { useCallback, useEffect, useRef, useState } from 'react'
import { currentRequestGeneration, errorMessage } from '../../api/client'
import type { SceneStructureRequestOptions } from '../../api/sceneStructure'
import { useAuthStore } from '../../stores/auth'
import { invalidateMismatchedEditorActor } from '../editorDrafts/verifyDraftScope'

/** 弹层关闭或作品授权变化后，旧请求无法写入当前界面。 */
export function useSceneStructureRequest(canAccess: () => boolean) {
  const pending = useRef<AbortController | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => () => pending.current?.abort(), [])

  const run = useCallback(async (
    action: (options: SceneStructureRequestOptions) => Promise<void>,
    onFailure?: (error: unknown) => void,
  ) => {
    const actor = useAuthStore.getState().user
    if (pending.current || !actor || !canAccess()) return

    const controller = new AbortController()
    const generation = currentRequestGeneration()
    const isCurrent = () => !controller.signal.aborted && canAccess()
      && generation === currentRequestGeneration()
      && useAuthStore.getState().user?.id === actor.id
      && useAuthStore.getState().user?.created_at === actor.created_at
    const options = { signal: controller.signal, isCurrent, editorActor: { id: actor.id, createdAt: actor.created_at } }
    pending.current = controller
    setBusy(true)
    setError('')

    try {
      await action(options)
    } catch (error) {
      invalidateMismatchedEditorActor(error, isCurrent)
      if (isCurrent()) {
        setError(errorMessage(error))
        onFailure?.(error)
      }
    } finally {
      pending.current = null
      if (isCurrent()) setBusy(false)
    }
  }, [canAccess])

  return { busy, error, setError, run }
}
