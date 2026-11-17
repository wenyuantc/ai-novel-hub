import type { NovelTemplateOverrides, NovelTemplateProjectRequest } from '../api/novelTemplates'

export interface TemplateCreationIntent {
  version: 1
  actor_id: number
  template_id: number
  /** 仅用于显示，不参与服务端的请求摘要。旧会话可能没有此字段。 */
  template_name?: string
  request: NovelTemplateProjectRequest
}

const keyFor = (actorId: number) => `ainovel.novel-template-intent.${actorId}`

/** 受理意图使用安全随机源；普通 HTTP 环境缺 randomUUID 时仍可生成 UUID v4。 */
export function createIntentKey(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID()
  if (!globalThis.crypto?.getRandomValues) throw new Error('当前浏览器无法生成安全的创建意图，请使用安全浏览器')
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function readOverrides(value: unknown): NovelTemplateOverrides | null {
  if (value === undefined) return {}
  if (!isRecord(value)) return null
  const result: NovelTemplateOverrides = {}
  const textFields = ['genre', 'premise', 'synopsis', 'theme', 'selling_points', 'audience'] as const
  if (Object.keys(value).some((key) => ![...textFields, 'tags', 'target_words'].includes(key))) return null
  for (const key of textFields) {
    if (value[key] !== undefined) {
      if (typeof value[key] !== 'string') return null
      result[key] = value[key]
    }
  }
  if (value.tags !== undefined) {
    if (!Array.isArray(value.tags) || value.tags.some((tag: unknown) => typeof tag !== 'string')) return null
    result.tags = value.tags
  }
  if (value.target_words !== undefined) {
    if (value.target_words !== null && (!Number.isSafeInteger(value.target_words) || Number(value.target_words) < 1000)) return null
    result.target_words = value.target_words === null ? null : Number(value.target_words)
  }
  return result
}

/** 只接纳当前账号的完整意图，旧账号和损坏的存储都不可用于建书。 */
export function readCreationIntent(actorId: number): TemplateCreationIntent | null {
  try {
    const raw = sessionStorage.getItem(keyFor(actorId))
    const data: unknown = raw ? JSON.parse(raw) : null
    if (!isRecord(data) || data.version !== 1 || data.actor_id !== actorId ||
        !Number.isSafeInteger(data.template_id) || Number(data.template_id) < 1 || !isRecord(data.request) ||
        (data.template_name !== undefined && (typeof data.template_name !== 'string' || !data.template_name.trim() || Array.from(data.template_name).length > 100))) return null
    const request = data.request
    if (Object.keys(request).some((key) => !['expected_revision', 'intent_key', 'title', 'pen_name_id', 'overrides'].includes(key)) ||
        !Number.isSafeInteger(request.expected_revision) || Number(request.expected_revision) < 1 ||
        typeof request.intent_key !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(request.intent_key) ||
        typeof request.title !== 'string' || !request.title.trim() || Array.from(request.title).length > 200 ||
        (request.pen_name_id !== null && (!Number.isSafeInteger(request.pen_name_id) || Number(request.pen_name_id) < 1))) return null
    const overrides = readOverrides(request.overrides)
    if (!overrides) return null
    return {
      version: 1, actor_id: actorId, template_id: Number(data.template_id),
      ...(typeof data.template_name === 'string' ? { template_name: data.template_name } : {}),
      request: { expected_revision: Number(request.expected_revision), intent_key: request.intent_key,
        title: request.title, pen_name_id: request.pen_name_id === null ? null : Number(request.pen_name_id),
        ...(request.overrides === undefined ? {} : { overrides }) },
    }
  } catch { return null }
}

/** 先持久化完整请求，再发送；存储不可用时调用方必须保留当前页面中的意图。 */
export function storeCreationIntent(intent: TemplateCreationIntent): boolean {
  try { sessionStorage.setItem(keyFor(intent.actor_id), JSON.stringify(intent)); return true } catch { return false }
}

export function clearCreationIntent(actorId: number, expectedKey?: string): boolean {
  try {
    if (expectedKey && readCreationIntent(actorId)?.request.intent_key !== expectedKey) return false
    sessionStorage.removeItem(keyFor(actorId))
    return true
  } catch { return false }
}

/** 成功且仍属于原账号和原意图时才接受响应；未知结果保留原请求供重试。 */
export async function sendCreationIntent<T>(intent: TemplateCreationIntent, send: () => Promise<T>, isCurrent: () => boolean): Promise<{ result: T; accepted: boolean }> {
  if (!isCurrent()) throw new Error('当前账号已变化，请重新打开建书窗口')
  const stored = readCreationIntent(intent.actor_id)
  if (stored && stored.request.intent_key !== intent.request.intent_key) throw new Error('创建请求已更新，请关闭并重新打开建书窗口。')
  if (!storeCreationIntent(intent)) throw new Error('浏览器无法保存重试信息，请允许本站的会话存储后重试。')
  const result = await send()
  const accepted = isCurrent() && clearCreationIntent(intent.actor_id, intent.request.intent_key)
  return { result, accepted }
}
