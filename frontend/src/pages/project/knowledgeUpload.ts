import { ApiError, errorMessage } from '../../api/client'
import { duplicateDocumentId, kbApi } from '../../api/kb'
import type { KbUploadResult, Project, Task, User } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { isProjectScopeCurrent, useProjectStore, type ProjectScope } from '../../stores/project'
import { projectAccess } from '../../utils/projectAccess'

/**
 * 上传入口：作者原流程；accepted 编辑者可上传（E6-c），用作者共享向量还是只建关键词索引由服务端
 * 按授权与作者配置决定（界面提示以 index-status 为准，前端不推算）；查看者、纯管理员不可上传。
 */
export function knowledgeUploadMode(project: Project | null, user: User | null, projectId: number): 'owner' | 'collaborator' | 'none' {
  const access = projectAccess(project, user, projectId)
  if (!access.canRead || !access.canEdit) return 'none'
  if (access.canUseOwnerAI) return 'owner'
  if (access.role === 'editor' && access.isCollaborator && access.canUseAIEntitled) return 'collaborator'
  return 'none'
}

/** 上传结果只对原登录及原授权轮次生效。 */
export async function uploadKnowledgeForScope(projectId: number, scope: ProjectScope | null, file: File,
  track: (task: Task) => void): Promise<KbUploadResult | null> {
  if (scope?.projectId !== projectId || !isProjectScopeCurrent(scope, 'canEdit')) return null
  const mode = knowledgeUploadMode(useProjectStore.getState().project, useAuthStore.getState().user, projectId)
  if (mode === 'none') return null
  const result = await kbApi.upload(projectId, file)
  if (!isProjectScopeCurrent(scope, 'canEdit')) return null
  if (result.document.project_id !== projectId || (result.task && result.task.project_id !== projectId)) {
    throw new Error('上传响应与当前作品不匹配，请刷新核对。')
  }
  if (result.task) track(result.task)
  return result
}

/** 文案只表述服务端已经确认的任务及文档状态。 */
export function knowledgeUploadNotice(result: KbUploadResult, collaborator = false): { type: 'success' | 'warning'; text: string } {
  const filename = result.document.filename
  if (result.task) {
    return { type: 'success', text: collaborator
      ? `《${filename}》已上传，索引任务已创建；使用作者共享的向量模型，费用由作者承担。`
      : `《${filename}》已上传，索引任务已创建。` }
  }
  if (result.task === null && result.document.status === 'ready_keyword_only') {
    return { type: 'success', text: `《${filename}》已上传，仅建立关键词索引；未调用作者向量模型，不产生相应费用。` }
  }
  return { type: 'warning', text: `《${filename}》已上传，索引状态待确认，请刷新查看。` }
}

/** 连接中断或服务端异常可能发生在提交之后，不自动重试。 */
export function knowledgeUploadError(error: unknown): { type: 'warning' | 'error'; text: string } {
  if (error instanceof ApiError && (error.status === 0 || error.status === 403 || error.status === 404 || error.status >= 500)) {
    return { type: 'warning', text: '上传结果未确认，请刷新文档列表核对后再操作。' }
  }
  return { type: duplicateDocumentId(error) === null ? 'error' : 'warning', text: errorMessage(error) }
}
