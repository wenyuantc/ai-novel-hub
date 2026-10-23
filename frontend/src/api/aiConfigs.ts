import { api } from './client'
import type {
  AIConfig,
  AIConfigCreate,
  AIConfigTestResult,
  AIConfigUpdate,
  ModelInfo,
  ModelListRequest,
  ModelType,
  ThinkingLevel,
} from './types'

export const aiConfigApi = {
  list: () => api.get<AIConfig[]>('/ai-configs'),
  create: (body: AIConfigCreate) => api.post<AIConfig>('/ai-configs', body),
  update: (id: number, body: AIConfigUpdate) => api.patch<AIConfig>(`/ai-configs/${id}`, body),
  remove: (id: number) => api.del(`/ai-configs/${id}`),
  setDefault: (id: number) => api.post<AIConfig>(`/ai-configs/${id}/default`),
  test: (id: number) => api.post<AIConfigTestResult>(`/ai-configs/${id}/test`),
  listModels: (body: ModelListRequest) =>
    api.post<{ models: ModelInfo[] }>('/ai-configs/models', body).then((r) => r.models),
}

export const PROVIDER_LABELS: Record<AIConfig['provider'], string> = {
  openai_compat: 'OpenAI 兼容',
  anthropic: 'Anthropic Claude',
  gemini: 'Google Gemini',
}

export const MODEL_TYPE_LABELS: Record<ModelType, string> = {
  text: '文本模型',
  image: '图片模型',
}

export const THINKING_LABELS: Record<ThinkingLevel, string> = {
  off: '关闭',
  low: 'low',
  medium: 'medium',
  high: 'high',
  xhigh: 'xhigh',
  max: 'max',
}
