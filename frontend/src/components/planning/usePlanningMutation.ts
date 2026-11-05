import { useState } from 'react'
import { errorMessage } from '../../api/client'
import type { PlanningRevision } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'

/** 写结果必须仍属于同一作品和授权；失败留在当前表单。 */
export function usePlanningMutation(projectId: number, onSaved: (revision: number) => void) {
  const { canEdit, canEditNow } = useProjectAccess(projectId)
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState<unknown>(null)
  const [error, setError] = useState('')
  const run = async <T extends PlanningRevision>(action: () => Promise<T>): Promise<T | undefined> => {
    if (saving || !canEditNow()) return
    setSaving(true); setError(''); setFailure(null)
    try {
      const result = await action()
      if (!canEditNow()) return
      onSaved(result.revision)
      return result
    } catch (err) {
      if (!canEditNow()) return
      setFailure(err); setError(errorMessage(err))
    } finally {
      if (canEditNow()) setSaving(false)
    }
  }
  return { canEdit, canEditNow, saving, failure, error, run, setError, clearError: () => { setFailure(null); setError('') } }
}
