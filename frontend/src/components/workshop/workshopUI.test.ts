import { App } from 'antd'
import { createElement, type ReactNode } from 'react'
import { renderToString } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { promptApi } from '../../api/prompts'
import type { PromptTemplateDetail, WorkshopItem } from '../../api/types'
import { WorkshopItemDetail } from './WorkshopItemDrawer'

const viewer = vi.hoisted(() => ({ isAdmin: false }))
const controls = vi.hoisted(() => ({ onImport: null as (() => void) | null, mounted: true }))
// SSR不执行effect；这里显式模拟已挂载的动作scope，保留真实点击后的API链。
vi.mock('../../hooks/useAuthorizationScope', () => ({ useAuthorizationScope: () => { let generation = 0; return { allowed: () => controls.mounted, begin: () => ++generation, current: (ticket: number) => controls.mounted && ticket === generation } } }))
vi.mock('./WorkshopItemControls', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./WorkshopItemControls')>()
  return { default: (props: Parameters<typeof actual.default>[0]) => {
    controls.onImport = props.onImport
    return createElement(actual.default, props)
  } }
})
vi.mock('../../stores/auth', () => ({
  useAuthStore: (select: (state: { user: import('../../api/types').User }) => unknown) => select({ user: { id: 1, username: '作者', created_at: '', is_admin: viewer.isAdmin, is_super_admin: viewer.isAdmin, effective_permissions: [], authorization_revision: 1, roles: [] } }),
}))
vi.mock('react-router-dom', () => ({
  Link: ({ to, children }: { to: string; children: ReactNode }) => createElement('a', { href: to }, children),
}))

const noop = () => {}
const item = (overrides: Partial<WorkshopItem> = {}): WorkshopItem => ({
  id: 1, kind: 'style', prompt_key: null, name: '雨夜风格', description: '短句叙事', category: 'suspense', tags: ['雨夜'], content: { pov: '第一人称' },
  author_name: 'alice', author_username: null, is_mine: false, is_anonymous: false, is_official: false,
  status: 'active', hidden_by: null, like_count: 3, download_count: 2, liked: false, imported: false,
  published_at: '2026-10-28T08:00:00', created_at: '2026-10-28T08:00:00', updated_at: '2026-10-28T08:00:00', ...overrides,
})
const render = (overrides: Partial<WorkshopItem> = {}) => renderToString(createElement(App, null,
  createElement(WorkshopItemDetail, { item: item(overrides), onItem: noop, onDeleted: noop, onUpdate: noop }),
)).replaceAll('<!-- -->', '')

describe('工坊详情渲染与权限', () => {
  it('上架详情可完整渲染，普通访客只有点赞与导入操作', () => {
    viewer.isAdmin = false
    const html = render()
    expect(html).toContain('雨夜风格')
    expect(html).toContain('点赞')
    expect(html).toContain('导入到我的')
    expect(html).not.toContain('作者下架')
    expect(html).not.toContain('管理员下架')
    expect(html).not.toContain('提交更新')
    expect(html).not.toContain('管理分类和标签')
  })
  it('本人下架详情正确展示下架来源，管理员下架时禁用作者恢复', () => {
    viewer.isAdmin = false
    expect(render({ status: 'hidden', hidden_by: 'author', is_mine: true })).toContain('作者下架')
    const html = render({ status: 'hidden', hidden_by: 'admin', is_mine: true })
    expect(html).toContain('管理员下架')
    expect(html).toContain('请联系管理员处理')
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*><span>重新上架<\/span><\/button>/)
  })
  it('管理员详情提供管理入口，匿名普通详情不补真实作者', () => {
    viewer.isAdmin = true
    expect(render()).toContain('管理分类和标签')
    viewer.isAdmin = false
    const html = render({ is_anonymous: true, author_name: null })
    expect(html).toContain('作者：匿名')
    expect(html).not.toContain('alice')
  })
  it('同一详情每次导入预览都重新读取当前模板，避免继续比较导入或恢复前的快照', async () => {
    const template: PromptTemplateDetail = {
      key: 'chapter.generate', name: '章节生成', category: 'chapter', category_label: '章节', description: '',
      overridden: false, enabled: false, outdated: false, updated_at: null,
      default_system: '默认系统', default_user: '默认用户', default_fingerprint: 'fp', variables: [], locked: [], override: null,
    }
    const get = vi.spyOn(promptApi, 'get').mockResolvedValue(template)
    try {
      render({ kind: 'prompt', prompt_key: template.key, content: { system: '工坊系统', user: '工坊用户' } })
      controls.onImport?.()
      await vi.waitFor(() => expect(get).toHaveBeenCalledTimes(1))
      controls.onImport?.()
      await vi.waitFor(() => expect(get).toHaveBeenCalledTimes(2))
      expect(get).toHaveBeenNthCalledWith(1, template.key)
      expect(get).toHaveBeenNthCalledWith(2, template.key)
    } finally { get.mockRestore() }
  })
})
