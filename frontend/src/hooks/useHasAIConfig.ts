import { useAIModelSource } from './useAIModelSource'

/**
 * 当前上下文能否发起文本 AI：加载中为 null。
 * 作品内无本人文本配置但已获作者模型授权的编辑者视为可用（留空回退作者默认配置）；作品外只看本人配置。
 */
export function useHasAIConfig(): boolean | null {
  const source = useAIModelSource('text')
  return source === 'loading' ? null : source !== 'none'
}
