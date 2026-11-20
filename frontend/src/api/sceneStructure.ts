import { request, type EditorActorIdentity } from './client'
import type {
  SceneMovePreview, SceneMoveResult, SceneMoveSelection, SceneMutation,
  SceneRestructureApply, SceneRestructurePreview, SceneRestructureRequest, SceneRestructureResult, Task,
} from './types'

export interface SceneStructureRequestOptions {
  signal: AbortSignal
  isCurrent: () => boolean
  editorActor: EditorActorIdentity
}

const moveBase = (pid: number, cid: number, sid: number) => `/projects/${pid}/chapters/${cid}/scenes/${sid}`
const suggestions = (pid: number, cid: number) => `/projects/${pid}/chapters/${cid}/scene-restructure`

export const sceneMoveApi = {
  preview: (pid: number, cid: number, sid: number, body: SceneMoveSelection, options: SceneStructureRequestOptions) =>
    request<SceneMovePreview>(`${moveBase(pid, cid, sid)}/move-preview`, { ...options, method: 'POST', body }),
  move: (pid: number, cid: number, sid: number, body: SceneMoveSelection & { signature: string }, options: SceneStructureRequestOptions) =>
    request<SceneMoveResult>(`${moveBase(pid, cid, sid)}/move`, { ...options, method: 'POST', body }),
}

export const sceneRestructureApi = {
  preview: (pid: number, cid: number, body: SceneRestructureRequest, options: SceneStructureRequestOptions) =>
    request<SceneRestructurePreview>(`${suggestions(pid, cid)}/preview`, { ...options, method: 'POST', body }),
  start: (pid: number, cid: number, body: SceneRestructureRequest & { signature: string }, options: SceneStructureRequestOptions) =>
    request<Task>(`${suggestions(pid, cid)}/suggestions`, { ...options, method: 'POST', body }),
  result: (pid: number, cid: number, taskKey: string, options: SceneStructureRequestOptions) =>
    request<SceneRestructureResult>(`${suggestions(pid, cid)}/suggestions/${encodeURIComponent(taskKey)}`, options),
  apply: (pid: number, cid: number, taskKey: string, body: SceneRestructureApply, options: SceneStructureRequestOptions) =>
    request<SceneMutation>(`${suggestions(pid, cid)}/suggestions/${encodeURIComponent(taskKey)}/apply`, { ...options, method: 'POST', body }),
}
