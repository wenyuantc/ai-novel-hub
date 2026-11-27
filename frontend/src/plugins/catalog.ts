import { dramaPlugin } from './drama'
import type { FeaturePlugin } from './types'

export const PLUGINS: readonly FeaturePlugin[] = [dramaPlugin]

/** 作品路由、任务中心共用插件登记；页面加载仍受 RequirePlugin 保护。 */
const tasks = new Map(PLUGINS.flatMap(plugin => Object.entries(plugin.tasks ?? {})))

export function pluginTaskInfo(kind: string) {
  return tasks.get(kind)
}
