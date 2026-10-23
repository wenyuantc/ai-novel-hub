import { useAIConfigStore } from '../stores/aiConfigs'

/** 是否存在可用的 AI 配置：加载中为 null。 */
export function useHasAIConfig(): boolean | null {
  const { configs, loaded } = useAIConfigStore()
  if (!loaded) return null
  return configs.length > 0
}
