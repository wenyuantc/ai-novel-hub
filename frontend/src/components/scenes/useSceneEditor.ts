import { useEffect, useLayoutEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import { currentRequestGeneration, watchRequestGeneration } from '../../api/client'
import { readChapterScenes, sceneApi } from '../../api/scenes'
import { richDocumentApi } from '../../api/richDocument'
import { useAuthStore } from '../../stores/auth'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore } from '../../stores/project'
import { SceneDraftController, sceneDraftDirty } from './sceneDraftController'
import { invalidateMismatchedEditorActor } from '../editorDrafts/verifyDraftScope'

const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

/** 账号、会话与章隔离控制器；同一会话授权变化只作废旧请求，保留仍可编辑的草稿。 */
export function useSceneEditor(projectId: number, chapterId: number, initialSceneId?: number) {
  const scopeVersion = useProjectStore((state) => state.scopeVersion)
  const status = useAuthStore((state) => state.status)
  const actorId = useAuthStore((state) => state.user?.id)
  const actorCreatedAt = useAuthStore((state) => state.user?.created_at)
  const generation = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const identity = `${status}:${actorId ?? ''}:${generation}`
  const controller = useMemo(() => {
    const actor = actorId !== undefined && actorCreatedAt ? { id: actorId, createdAt: actorCreatedAt } : undefined
    const belongs = () => {
      const state = useAuthStore.getState()
      return identity === `${state.status}:${state.user?.id ?? ''}:${currentRequestGeneration()}`
        && state.user?.created_at === actorCreatedAt
    }
    const write = async <T,>(operation: () => Promise<T>, isCurrent?: () => boolean) => {
      try { return await operation() }
      catch (error) {
        invalidateMismatchedEditorActor(error, isCurrent ?? belongs)
        throw error
      }
    }
    return new SceneDraftController(chapterId, {
      list: (signal, isCurrent) => readChapterScenes(projectId, chapterId, signal, isCurrent),
      get: (sid, signal, isCurrent) => sceneApi.get(projectId, chapterId, sid, signal, isCurrent),
      update: (sid, body, isCurrent) => write(() => sceneApi.update(projectId, chapterId, sid, body, isCurrent, actor), isCurrent),
      getDocument: (sid, signal, isCurrent) => richDocumentApi.get(projectId, chapterId, sid, signal, isCurrent),
      saveDocument: (sid, body, isCurrent) => write(() => richDocumentApi.save(projectId, chapterId, sid, body, undefined, isCurrent, actor), isCurrent),
    }, () => belongs() && isProjectScopeCurrent(captureProjectScope(projectId), 'canRead'),
    () => !!actor && belongs() && isProjectScopeCurrent(captureProjectScope(projectId), 'canEdit'),
    () => useProjectStore.getState().scopeVersion)
  }, [projectId, chapterId, identity, actorId, actorCreatedAt])
  useLayoutEffect(() => {
    controller.authorizationChanged()
  }, [controller, scopeVersion])
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot)
  const requested = useRef(initialSceneId)
  useEffect(() => { requested.current = initialSceneId }, [initialSceneId])
  useEffect(() => {
    controller.activate()
    void controller.load(requested.current)
    return () => controller.deactivate()
  }, [controller])
  // 已经打开后的场景切换由章节页的保存屏障调度。
  return { ...snapshot, dirty: sceneDraftDirty(snapshot), controller }
}
