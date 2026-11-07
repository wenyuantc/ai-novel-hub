import { ApiError, request } from './client'
import type {
  Scene, SceneCreate, SceneMerge, SceneMergePreview, SceneMutation, ScenePage,
  ScenePositionResolution, SceneReferenceBlocker, SceneReorder, SceneSettingContext, SceneSplit, SceneUpdate,
  GenerateSceneRequest, PolishSceneRequest, ReviseSceneRequest, SceneRewriteRequest, SceneRewriteDone, SceneRewriteApply, Task,
} from './types'
import { runGeneration } from './sse'

const base = (pid: number, cid: number) => `/projects/${pid}/chapters/${cid}/scenes`
export interface SceneListQuery { offset?: number; limit?: number }

/** 场景正文与结构统一提交用户已核对的章内容版本；失败不重放。 */
export const sceneApi = {
  list: (pid: number, cid: number, query: SceneListQuery = {}, signal?: AbortSignal) =>
    request<ScenePage>(base(pid, cid), { query: { ...query }, signal }),
  directory: (pid: number, query: SceneListQuery = {}, signal?: AbortSignal) =>
    request<ScenePage>(`/projects/${pid}/scenes`, { query: { ...query }, signal }),
  get: (pid: number, cid: number, sid: number, signal?: AbortSignal) =>
    request<Scene>(`${base(pid, cid)}/${sid}`, { signal }),
  create: (pid: number, cid: number, body: SceneCreate) =>
    request<Scene>(base(pid, cid), { method: 'POST', body }),
  update: (pid: number, cid: number, sid: number, body: SceneUpdate) =>
    request<Scene>(`${base(pid, cid)}/${sid}`, { method: 'PATCH', body }),
  reorder: (pid: number, cid: number, body: SceneReorder) =>
    request<SceneMutation>(`${base(pid, cid)}/reorder`, { method: 'POST', body }),
  split: (pid: number, cid: number, sid: number, body: SceneSplit) =>
    request<SceneMutation>(`${base(pid, cid)}/${sid}/split`, { method: 'POST', body }),
  mergePreview: (pid: number, cid: number, body: SceneReorder) =>
    request<SceneMergePreview>(`${base(pid, cid)}/merge-preview`, { method: 'POST', body }),
  merge: (pid: number, cid: number, body: SceneMerge) =>
    request<SceneMutation>(`${base(pid, cid)}/merge`, { method: 'POST', body }),
  remove: (pid: number, cid: number, sid: number, revision: number) =>
    request<SceneMutation>(`${base(pid, cid)}/${sid}`, { method: 'DELETE', query: { expected_content_revision: revision } }),
  resolvePosition: (pid: number, cid: number, fingerprint: string, start: number, end: number, signal?: AbortSignal) =>
    request<ScenePositionResolution>(`${base(pid, cid)}/resolve-position`, { query: { fingerprint, start, end }, signal }),
  settingContext: (pid: number, cid: number, sid: number, signal?: AbortSignal) =>
    request<SceneSettingContext>(`${base(pid, cid)}/${sid}/setting-context`, { signal }),
  generate: (pid: number, cid: number, sid: number, body: GenerateSceneRequest) => request<Task>(`${base(pid, cid)}/${sid}/generate`, { method: 'POST', body }),
  polish: (pid: number, cid: number, sid: number, body: PolishSceneRequest) => request<Task>(`${base(pid, cid)}/${sid}/polish`, { method: 'POST', body }),
  revise: (pid: number, cid: number, sid: number, body: ReviseSceneRequest) => request<Task>(`${base(pid, cid)}/${sid}/revise`, { method: 'POST', body }),
  rewrite: (pid: number, cid: number, sid: number, body: SceneRewriteRequest, onDelta: (text: string) => void, signal?: AbortSignal) =>
    runGeneration<SceneRewriteDone>(`${base(pid, cid)}/${sid}/rewrite`, body, { onDelta, signal, parseDone: parseSceneRewriteDone }),
  applyRewrite: (pid: number, cid: number, sid: number, body: SceneRewriteApply) => request<Scene>(`${base(pid, cid)}/${sid}/rewrite/apply`, { method: 'POST', body }),
}

/** SSE 是不可信边界，缺少真实目标或版本不能进入“采纳”。 */
export function parseSceneRewriteDone(value: unknown): SceneRewriteDone {
  if (!value || typeof value !== 'object') throw new Error('场景改写结果格式不正确')
  const v = value as Record<string, unknown>
  const integer = (n: unknown, min: number): n is number => typeof n === 'number' && Number.isSafeInteger(n) && n >= min
  if (typeof v.text !== 'string' || typeof v.original !== 'string' || typeof v.before !== 'string' || !integer(v.start, 0) || !integer(v.end, v.start) || !integer(v.scene_id, 1) || !integer(v.chapter_id, 1) || !integer(v.content_revision, 0) || !integer(v.planning_revision, 0) || !(v.log_id === null || integer(v.log_id, 1))) throw new Error('场景改写结果缺少目标或版本，请重新生成')
  return { text: v.text, original: v.original, before: v.before, start: v.start, end: v.end, scene_id: v.scene_id, chapter_id: v.chapter_id, content_revision: v.content_revision, planning_revision: v.planning_revision, log_id: v.log_id }
}

/** 排序必须先收齐同一版本的轻量身份；页间版本变化不能拼成伪完整列表。 */
export async function readChapterScenes(pid: number, cid: number, signal?: AbortSignal): Promise<ScenePage> {
  const first = await sceneApi.list(pid, cid, { offset: 0, limit: 100 }, signal)
  const items = [...first.items]
  while (items.length < first.total) {
    const next = await sceneApi.list(pid, cid, { offset: items.length, limit: 100 }, signal)
    if (next.content_revision !== first.content_revision || next.planning_revision !== first.planning_revision || next.total !== first.total) {
      throw new ApiError(409, '加载期间场景顺序或规划已变化，请重新加载后核对。', { code: 'scene_preview_changed' })
    }
    if (!next.items.length) throw new Error('场景目录未完整返回，请重试。')
    items.push(...next.items)
  }
  if (new Set(items.map((item) => item.id)).size !== items.length) throw new Error('场景目录包含重复位置，请重新加载。')
  return { ...first, items }
}

/** 只收窄错误中的公开阻塞摘要，不把未知对象直接渲染或强转。 */
export function sceneBlockers(error: unknown): SceneReferenceBlocker[] {
  if (!(error instanceof ApiError) || !Array.isArray(error.body?.blockers)) return []
  return error.body.blockers.flatMap((value: unknown) => {
    if (!value || typeof value !== 'object' || !('kind' in value) || !('id' in value) || !('label' in value)) return []
    return typeof value.kind === 'string' && typeof value.id === 'number' && typeof value.label === 'string'
      ? [{ kind: value.kind, id: value.id, label: value.label }] : []
  })
}
