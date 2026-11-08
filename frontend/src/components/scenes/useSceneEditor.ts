import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import { readChapterScenes, sceneApi } from '../../api/scenes'
import { richDocumentApi } from '../../api/richDocument'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import { SceneDraftController, sceneDraftDirty } from './sceneDraftController'

/** 授权和章身份发生变化时重建控制器，卸载只丢弃本页响应。 */
export function useSceneEditor(projectId: number, chapterId: number, initialSceneId?: number) {
  const { canReadNow, canEditNow } = useProjectAccess(projectId)
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
  const controller = useMemo(() => {
    const belongs = () => { const state = useAuthStore.getState(); return identity === `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}` }
    return new SceneDraftController(chapterId, {
    list: (signal) => readChapterScenes(projectId, chapterId, signal),
    get: (sid, signal) => sceneApi.get(projectId, chapterId, sid, signal),
    update: (sid, body) => sceneApi.update(projectId, chapterId, sid, body),
    getDocument: (sid, signal) => richDocumentApi.get(projectId, chapterId, sid, signal),
    saveDocument: (sid, body) => richDocumentApi.save(projectId, chapterId, sid, body),
  }, () => belongs() && canReadNow(), () => belongs() && canEditNow())
  }, [projectId, chapterId, identity, canReadNow, canEditNow])
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
