import { generateCover, uploadCover } from '../../api/projects'
import type { AIConfig, CoverGenerateRequest, CoverPreview, Project, ProjectAccess, User } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { isProjectScopeCurrent, useProjectStore, type ProjectScope } from '../../stores/project'
import { projectAccess } from '../../utils/projectAccess'
import { coverPreviewBlob } from '../../utils/projectCover'

/** 封面只开放给作者及服务端确认拥有 AI 能力的真实编辑者。 */
export function coverAvailability(project: Project | null, user: User | null, projectId: number): boolean {
  const access = projectAccess(project, user, projectId)
  return access.canRead && access.canEdit && access.canUseAIEntitled
    && (access.role === 'owner' || (access.role === 'editor' && access.isCollaborator))
}

export type CoverChoice =
  | { allowed: false; mode: 'loading' | 'missing' | 'invalid' }
  | { allowed: true; mode: 'actor'; configId: number }
  | { allowed: true; mode: 'owner' }

/** 本人配置存在时明确发送本人 ID，避免配置被移除后意外切到作者费用。 */
export function coverChoice(access: ProjectAccess | null | undefined, loaded: boolean, configs: AIConfig[], selectedId: number | null): CoverChoice {
  if (!loaded) return { allowed: false, mode: 'loading' }
  const images = configs.filter((config) => config.model_type === 'image')
  if (selectedId !== null) {
    return images.some((config) => config.id === selectedId)
      ? { allowed: true, mode: 'actor', configId: selectedId } : { allowed: false, mode: 'invalid' }
  }
  if (images.length) {
    const chosen = [...images].sort((a, b) => Number(b.is_default) - Number(a.is_default) || b.id - a.id)[0]
    return { allowed: true, mode: 'actor', configId: chosen.id }
  }
  return access?.role === 'editor' && access.is_collaborator && access.allow_owner_ai
    ? { allowed: true, mode: 'owner' } : { allowed: false, mode: 'missing' }
}

/** 请求时所选来源必须与用户在按钮上看到的选择一致。 */
export function sameCoverChoice(displayed: CoverChoice, current: CoverChoice): boolean {
  if (!displayed.allowed || !current.allowed || displayed.mode !== current.mode) return false
  return displayed.mode === 'owner' || (current.mode === 'actor' && displayed.configId === current.configId)
}

export function currentCoverScope(scope: ProjectScope | null, projectId: number): boolean {
  const auth = useAuthStore.getState()
  return !!scope && scope.projectId === projectId && isProjectScopeCurrent(scope, 'canUseAIEntitled')
    && isProjectScopeCurrent(scope, 'canEdit') && coverAvailability(useProjectStore.getState().project, auth.user, projectId)
}

/** 不使用新作用域接收旧响应，且仅把允许展示的图片和来源传给界面。 */
export async function requestCoverForScope(projectId: number, scope: ProjectScope | null, body: CoverGenerateRequest,
  mode: 'actor' | 'owner', signal: AbortSignal): Promise<CoverPreview | null> {
  if (!currentCoverScope(scope, projectId) || signal.aborted) return null
  if (mode === 'actor' ? !Number.isSafeInteger(body.config_id) || (body.config_id ?? 0) <= 0 : body.config_id !== undefined) {
    throw new Error('图片模型选择已变化，请重新确认。')
  }
  const result = await generateCover(projectId, body, signal)
  if (!currentCoverScope(scope, projectId) || signal.aborted) return null
  const access = useProjectStore.getState().project?.access
  const source = result?.model_source
  if (typeof result?.image_base64 !== 'string' || typeof result.mime_type !== 'string'
    || (source != null && source !== 'actor' && source !== 'owner')
    || (access?.role === 'editor' && source !== 'actor' && source !== 'owner')
    || (source === 'owner' && (mode !== 'owner' || access?.role !== 'editor' || !access.allow_owner_ai))) {
    throw new Error('图片模型来源无法确认，请刷新后重新操作。')
  }
  return { image_base64: result.image_base64, mime_type: result.mime_type, ...(source ? { model_source: source } : {}) }
}

/** 旧预览或旧上传响应不能在新授权轮次应用。 */
export async function saveCoverForScope(projectId: number, scope: ProjectScope | null, preview: CoverPreview): Promise<Project | null> {
  if (!currentCoverScope(scope, projectId)) return null
  const updated = await uploadCover(projectId, coverPreviewBlob(preview))
  return currentCoverScope(scope, projectId) ? updated : null
}
