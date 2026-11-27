import { lazy } from 'react'
import type { RouteObject } from 'react-router-dom'
import type { NavGroup } from '../components/StudioNavigation'
import RequirePlugin from './RequirePlugin'
import type { FeaturePlugin } from './types'

export const PLUGINS: readonly FeaturePlugin[] = []

export function pluginRoutes(plugins: readonly FeaturePlugin[], scope: 'project' | 'global'): RouteObject[] {
  const ids = new Set<string>()
  return plugins.flatMap((plugin) => {
    if (!/^[a-z][a-z0-9_]{1,31}$/.test(plugin.id) || ids.has(plugin.id)) {
      throw new Error('插件标识无效或重复：' + plugin.id)
    }
    ids.add(plugin.id)
    const paths = new Set<string>()
    return (plugin[scope] ?? []).map(({ path, load }) => {
      const segments = path.split('/')
      if (segments[0] !== plugin.id || segments.some((part) => !part || part === '.' || part === '..') || paths.has(path.toLowerCase())) {
        throw new Error('插件页面路径无效或重复：' + path)
      }
      paths.add(path.toLowerCase())
      // Router 的 route.lazy 会在父守卫渲染前执行。React.lazy 等启用后才请求页面。
      const Page = lazy(load)
      return { path: 'features/' + path, element: <RequirePlugin id={plugin.id}><Page /></RequirePlugin> }
    })
  })
}

export function withPluginNav(groups: NavGroup[], plugins: readonly FeaturePlugin[], enabled: readonly string[], projectId: number | null): NavGroup[] {
  const result = groups.map((group) => ({ ...group, items: [...group.items] }))
  const scope = projectId === null ? 'global' : 'project'
  const prefix = projectId === null ? '/' : '/projects/' + projectId + '/'
  for (const plugin of plugins) {
    if (!enabled.includes(plugin.id)) continue
    for (const { path, nav } of plugin[scope] ?? []) {
      if (!nav) continue
      let group = result.find(({ title }) => title === nav.group)
      if (!group) {
        group = { title: nav.group, items: [] }
        result.push(group)
      }
      group.items.push({ to: prefix + 'features/' + path, label: nav.label, icon: nav.icon })
    }
  }
  return result
}
