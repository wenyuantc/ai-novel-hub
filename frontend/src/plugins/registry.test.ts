import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { useAuthStore } from '../stores/auth'
import { pluginRoutes, withPluginNav } from './registry'
import type { FeaturePlugin } from './types'

vi.mock('../stores/auth', async (original) => {
  const actual = await original<typeof import('../stores/auth')>()
  const hook = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { useAuthStore: Object.assign(hook, actual.useAuthStore) }
})
const load = vi.fn(async () => ({ default: () => '作者页面' }))
const sample: FeaturePlugin = {
  id: 'sample',
  project: [{ path: 'sample/editor', load, nav: { group: '作品', label: '编辑', icon: null } }],
  global: [{ path: 'sample', load, nav: { group: '工作', label: '工作台', icon: null } }],
}
describe('插件入口', () => {
  it('按作用域合并已启用的导航，保留原分组', () => {
    const groups = [{ title: '作品', items: [] }]
    expect(withPluginNav(groups, [sample], [], 42)).toEqual(groups)
    expect(withPluginNav(groups, [sample], ['sample'], 42)[0].items[0].to).toBe('/projects/42/features/sample/editor')
    expect(withPluginNav([], [sample], ['sample'], null)[0].items[0].to).toBe('/features/sample')
    expect(groups[0].items).toEqual([])
    expect(pluginRoutes([], 'project')).toEqual([])
  })
  it('重复或非法标识在登记阶段报错，即使没有提供同一作用域的页面', () => {
    expect(() => pluginRoutes([sample, { id: sample.id }], 'global')).toThrow(/重复.*sample/)
    expect(() => pluginRoutes([{ id: '../sample' }], 'project')).toThrow(/无效/)
    expect(() => pluginRoutes([{ id: 'sample', global: [{ path: 'sample/../settings', load }] }], 'global')).toThrow(/路径无效/)
  })
  it('插件标识与核心路径同名时仍使用独立地址', () => {
    expect(pluginRoutes([{ id: 'projects', global: [{ path: 'projects', load }] }], 'global')[0].path).toBe('features/projects')
    expect(pluginRoutes([{ id: 'overview', project: [{ path: 'overview', load }] }], 'project')[0].path).toBe('features/overview')
  })
  it('配置加载中与停用时，直接访问不下载页面', () => {
    const [route] = pluginRoutes([sample], 'project')
    useAuthStore.setState({ pluginConfigReady: false, enabledPlugins: [] })
    expect(renderToStaticMarkup(route.element)).toContain('正在读取功能插件状态')
    useAuthStore.setState({ pluginConfigReady: true })
    expect(renderToStaticMarkup(route.element)).toContain('功能插件未启用')
    expect(load).not.toHaveBeenCalled()
    useAuthStore.setState({ enabledPlugins: ['sample'] })
    renderToStaticMarkup(route.element)
    expect(load).toHaveBeenCalledOnce()
  })
})
