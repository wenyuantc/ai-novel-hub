import { useParams } from 'react-router-dom'
import { useShallow } from 'zustand/react/shallow'
import type { ModelType, Project } from '../api/types'
import { useAIConfigStore } from '../stores/aiConfigs'
import { useAuthStore } from '../stores/auth'
import { useProjectStore } from '../stores/project'
import { aiModelSource, projectAccess, type AIModelSource } from '../utils/projectAccess'

/**
 * 当前路由上下文的 AI 模型来源：作品路由（:pid）内按作品能力快照判定是否可回退作者授权模型；
 * 作品外（账号级页面）与向量模型只看本人配置，行为不变。
 */
export function useAIModelSource(modelType: ModelType = 'text', projectSnapshot?: Project): AIModelSource {
  const { pid } = useParams()
  const projectId = Number(pid)
  const user = useAuthStore((s) => s.user)
  const project = useProjectStore((s) => s.project)
  const loaded = useAIConfigStore((s) => s.loaded)
  const count = useAIConfigStore(useShallow((s) => s.configs.filter((c) => c.model_type === modelType))).length
  const scoped = pid !== undefined && Number.isSafeInteger(projectId) && projectId > 0 && modelType !== 'embedding'
  return aiModelSource(scoped ? projectAccess(projectSnapshot ?? project, user, projectId) : null, loaded, count)
}
