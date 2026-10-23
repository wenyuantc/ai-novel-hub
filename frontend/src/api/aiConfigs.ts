import { api } from './client'
import type { AIConfig, AIConfigCreate, AIConfigTestResult, AIConfigUpdate } from './types'

export const aiConfigApi = {
  list: () => api.get<AIConfig[]>('/ai-configs'),
  create: (body: AIConfigCreate) => api.post<AIConfig>('/ai-configs', body),
  update: (id: number, body: AIConfigUpdate) => api.patch<AIConfig>(`/ai-configs/${id}`, body),
  remove: (id: number) => api.del(`/ai-configs/${id}`),
  setDefault: (id: number) => api.post<AIConfig>(`/ai-configs/${id}/default`),
  test: (id: number) => api.post<AIConfigTestResult>(`/ai-configs/${id}/test`),
}

export const PROVIDER_LABELS: Record<AIConfig['provider'], string> = {
  openai_compat: 'OpenAI 兼容',
  anthropic: 'Anthropic Claude',
  gemini: 'Google Gemini',
}
