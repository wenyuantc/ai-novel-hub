type SecretAction = 'show' | 'copy'
type RevealKey = (signal: AbortSignal) => Promise<string>
type CopyKey = (key: string) => Promise<boolean>

export type ApiKeySecretResult = { request: number } & (
  | { kind: 'visible'; key: string }
  | { kind: 'copied' }
  | { kind: 'manual'; key: string }
)

/** 剪贴板受限时返回 false，由界面提供可选择的文本，绝不输出密钥到错误消息。 */
export async function writeKeyToClipboard(key: string): Promise<boolean> {
  try {
    if (typeof navigator === 'undefined' || !navigator.clipboard?.writeText) return false
    await navigator.clipboard.writeText(key)
    return true
  } catch {
    return false
  }
}

/** 页面局部的密钥请求范围；取消后丢弃迟到结果，也阻止它继续写剪贴板。 */
export function createApiKeySecretSession(reveal: RevealKey, copy: CopyKey = writeKeyToClipboard) {
  let request = 0
  let active: AbortController | null = null

  const cancel = () => {
    request += 1
    active?.abort()
    active = null
  }

  return {
    cancel,
    isCurrent: (result: ApiKeySecretResult) => result.request === request,
    async run(action: SecretAction, knownKey?: string): Promise<ApiKeySecretResult | null> {
      cancel()
      const ticket = request
      const controller = new AbortController()
      active = controller
      const current = () => ticket === request && !controller.signal.aborted
      try {
        const key = knownKey ?? await reveal(controller.signal)
        if (!current()) return null
        if (action === 'show') return { request: ticket, kind: 'visible', key }
        const copied = await copy(key)
        if (!current()) return null
        return copied ? { request: ticket, kind: 'copied' } : { request: ticket, kind: 'manual', key }
      } catch (err) {
        if (!current()) return null
        throw err
      } finally {
        if (active === controller) active = null
      }
    },
  }
}
