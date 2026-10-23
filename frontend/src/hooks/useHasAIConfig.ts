import { useShallow } from 'zustand/react/shallow'
import { selectTextConfigs, useAIConfigStore } from '../stores/aiConfigs'

/** 是否存在可用的文本模型配置：加载中为 null。 */
export function useHasAIConfig(): boolean | null {
  const configs = useAIConfigStore(useShallow(selectTextConfigs))
  const loaded = useAIConfigStore((s) => s.loaded)
  if (!loaded) return null
  return configs.length > 0
}
