import type { ComponentType, ReactNode } from 'react'

export interface PluginPage {
  /** 相对于作品或全站根路径，首段为插件标识。 */
  path: string
  load: () => Promise<{ default: ComponentType }>
  nav?: { group: string; label: string; icon: ReactNode }
}

export interface FeaturePlugin {
  id: string
  project?: readonly PluginPage[]
  global?: readonly PluginPage[]
}
