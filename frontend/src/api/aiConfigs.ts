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
  volcengine: '火山引擎 Seedance',
  minimax: 'MiniMax',
  aliyun_wan: '阿里云百炼 Wan',
}

export const MODEL_TYPE_LABELS: Record<ModelType, string> = {
  text: '文本模型',
  image: '图片模型',
  embedding: '向量模型',
  video: '视频模型',
}

const PROVIDER_MODEL_TYPES: Record<AIConfig['provider'], readonly ModelType[]> = {
  openai_compat: ['text', 'image', 'embedding', 'video'],
  anthropic: ['text', 'image'],
  gemini: ['text', 'image', 'embedding'],
  volcengine: ['video'],
  minimax: ['video'],
  aliyun_wan: ['video'],
}

export function modelTypesFor(provider?: AIConfig['provider']): readonly ModelType[] {
  return provider ? PROVIDER_MODEL_TYPES[provider] : Object.keys(MODEL_TYPE_LABELS) as ModelType[]
}

export function providersFor(type: ModelType) {
  return (Object.keys(PROVIDER_LABELS) as AIConfig['provider'][]).filter((p) => modelTypesFor(p).includes(type))
}

export const EMBEDDING_HELP = '用于知识库与剧情记忆的语义检索，不配置时使用关键词检索'

/** 从向量模型连通性测试的 message（「连接成功，向量维度 1536」）读出维度；没有时为 null */
export function embeddingDimension(message: string): number | null {
  const m = /向量维度\s*(\d+)/.exec(message)
  return m ? Number(m[1]) : null
}

export const THINKING_LABELS: Record<ThinkingLevel, string> = {
  off: '关闭',
  low: 'low',
  medium: 'medium',
  high: 'high',
  xhigh: 'xhigh',
  max: 'max',
}
