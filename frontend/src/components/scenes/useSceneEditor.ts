import { useEffect, useLayoutEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import { currentRequestGeneration, watchRequestGeneration } from '../../api/client'
import { readChapterScenes, sceneApi } from '../../api/scenes'
import { richDocumentApi } from '../../api/richDocument'
import { useAuthStore } from '../../stores/auth'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore } from '../../stores/project'
import { SceneDraftController, sceneDraftDirty } from './sceneDraftController'

const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

/** 账号、会话与章隔离控制器；同一会话授权变化只作废旧请求，保留仍可编辑的草稿。 */
export function useSceneEditor(projectId: number, chapterId: number, initialSceneId?: number) {
  const scopeVersion = useProjectStore((state) => state.scopeVersion)
  const status = useAuthStore((state) => state.status)
  const actorId = useAuthStore((state) => state.user?.id)
  const generation = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const identity = `${status}:${actorId ?? ''}:${generation}`
  const controller = useMemo(() => {
    const belongs = () => {
      const state = useAuthStore.getState()
      return identity === `${state.status}:${state.user?.id ?? ''}:${currentRequestGeneration()}`
    }
    return new SceneDraftController(chapterId, {
      list: (signal) => readChapterScenes(projectId, chapterId, signal),
      get: (sid, signal) => sceneApi.get(projectId, chapterId, sid, signal),
      update: (sid, body) => sceneApi.update(projectId, chapterId, sid, body),
      getDocument: (sid, signal) => richDocumentApi.get(projectId, chapterId, sid, signal),
      saveDocument: (sid, body) => richDocumentApi.save(projectId, chapterId, sid, body),
    }, () => belongs() && isProjectScopeCurrent(captureProjectScope(projectId), 'canRead'),
    () => belongs() && isProjectScopeCurrent(captureProjectScope(projectId), 'canEdit'),
    () => useProjectStore.getState().scopeVersion)
  }, [projectId, chapterId, identity])
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
  const lastQuery = useRef<{ controller: SceneDraftController; id: number }>()
  useEffect(() => {
    if (initialSceneId === undefined || snapshot.loading || !snapshot.scene || (lastQuery.current?.controller === controller && lastQuery.current.id === initialSceneId)) return
    lastQuery.current = { controller, id: initialSceneId }
    if (snapshot.scene.id !== initialSceneId) void controller.select(initialSceneId)
  }, [controller, initialSceneId, snapshot.loading, snapshot.scene])
  return { ...snapshot, dirty: sceneDraftDirty(snapshot), controller }
}
