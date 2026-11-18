import { useEffect, useLayoutEffect, useMemo, useSyncExternalStore } from 'react'
import type { Chapter } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { captureProjectScope, useProjectStore } from '../../stores/project'
import type { SceneDraftController } from '../scenes/sceneDraftController'
import { ChapterAutosave } from './chapterAutosave'

export function useChapterAutosave(
  initial: Chapter,
  scene: SceneDraftController,
  blocked: boolean,
  onChapter: (chapter: Chapter) => void,
  onSaved: () => void,
) {
  const actorId = useAuthStore((s) => s.user?.id)
  const actorBorn = useAuthStore((s) => s.user?.created_at)
  const scopeVersion = useProjectStore((s) => s.scopeVersion)
  const projectId = useProjectStore((s) => s.project?.id)
  const projectBorn = useProjectStore((s) => s.project?.created_at)
  const autosave = useMemo(() => {
    const access = captureProjectScope(initial.project_id)
    const scope = access?.version === scopeVersion && actorId !== undefined && actorBorn && projectBorn && projectId === initial.project_id
      ? { actor_id: actorId, actor_created_at: actorBorn, project_id: projectId, project_created_at: projectBorn }
      : null
    return new ChapterAutosave(initial, scene, access, scope)
  }, [initial, scene, scopeVersion, actorId, actorBorn, projectId, projectBorn])
  useLayoutEffect(() => { autosave.configureCallbacks(onChapter, onSaved) }, [autosave, onChapter, onSaved])
  useLayoutEffect(() => { autosave.setBlocked(blocked) }, [autosave, blocked])
  useEffect(() => autosave.activate(), [autosave])
  useSyncExternalStore(autosave.subscribe, autosave.getSnapshot, autosave.getSnapshot)
  return autosave
}
