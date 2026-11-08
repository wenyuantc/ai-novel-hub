// 提示词组件：页面与编辑器的服务端渲染（node 环境）——列表引用数、使用关系跳转、冲突 / 占用 / 影响提示、引用组件与插入入口
import { App } from 'antd'
import { createElement, type ReactElement } from 'react'
import { renderToString } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { PromptComponent, PromptComponentBrief, PromptTemplateDetail, Skill, User } from '../api/types'
import { skillFixture } from '../utils/skills.test'

const loader = vi.hoisted(() => ({ data: undefined as unknown }))
const auth = vi.hoisted(() => ({ superAdmin: false }))
vi.mock('../hooks/useLoader', () => ({
  useLoader: () => ({ data: loader.data, loading: false, error: null, reload: () => {}, setData: () => {} }),
}))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => true, useModalWidth: () => 'calc(100vw - 24px)' }))
vi.mock('../stores/auth', () => {
  const state = () => ({ status: 'authenticated', user: { id: 1, username: '作者', created_at: '', is_admin: auth.superAdmin, is_super_admin: auth.superAdmin, effective_permissions: [], authorization_revision: 1, roles: [] } as User })
  const useAuthStore = (select: (s: ReturnType<typeof state>) => unknown) => select(state())
  useAuthStore.getState = state
  return { useAuthStore }
})

const { default: PromptComponentsPage } = await import('./PromptComponentsPage')
const { ComponentForm, ComponentRejectionAlert } = await import('../components/promptComponents/ComponentEditDrawer')
const { SkillForm } = await import('../components/skills/SkillEditDrawer')
const { default: PromptEditor } = await import('../components/prompts/PromptEditor')

const render = (el: ReactElement) =>
  renderToString(createElement(MemoryRouter, null, createElement(App, null, el))).replaceAll('<!-- -->', '')
const text = (html: string) => html.replace(/<[^>]+>/g, '').replace(/\s/g, '')
const noop = () => {}

const brief = (over: Partial<PromptComponentBrief> = {}): PromptComponentBrief => ({
  id: 1, slug: 'banned-words', name: '禁用词', description: '本书禁用的词', revision: 3, content_chars: 12, usage_count: 2,
  created_at: '2026-11-08T08:00:00Z', updated_at: '2026-11-08T08:00:00Z', ...over,
})
const component: PromptComponent = {
  ...brief(),
  content: '不要写「然而」',
  usages: [
    { kind: 'skill', id: 5, key: null, name: '长篇续写', enabled: null },
    { kind: 'override', id: null, key: 'chapter.generate', name: '写章节', enabled: false },
  ],
}

describe('组件库页面', () => {
  it('列表显示名称、标识、说明、引用数，带搜索、新建、导入导出入口', () => {
    loader.data = [brief(), brief({ id: 2, slug: 'dialogue', name: '对话写法', description: '', usage_count: 0 })]
    const html = render(createElement(PromptComponentsPage))
    const t = text(html)
    for (const v of ['提示词组件', '禁用词', 'banned-words', '本书禁用的词', '引用2', '对话写法', '引用0', '暂无说明', '新建组件', '导入', '导出', '{{&gt;标识}}'])
      expect(t).toContain(v)
    expect(html).toContain('aria-label="搜索组件"')
    expect(html).toContain('data-component-id="2"')
  })

  it('详情列出使用关系并可跳转到 Skill 抽屉与模板覆盖编辑页，显示覆盖是否启用', () => {
    const html = render(createElement(ComponentForm, { open: true, componentId: 1, onClose: noop, onSaved: noop, component }))
    expect(text(html)).toContain('使用关系（2）')
    expect(html).toContain('href="/skills?skill=5"')
    expect(html).toContain('href="/prompt-templates?key=chapter.generate"')
    expect(text(html)).toContain('模板覆盖（已停用）')
    expect(text(html)).toContain('{{&gt;banned-words}}')
  })

  it('修订冲突提示保留草稿并提供加载最新版本；被引用 / 影响覆盖时逐条列出', () => {
    const stale = text(render(createElement(ComponentRejectionAlert, { rejection: { kind: 'stale' }, onReload: noop })))
    expect(stale).toContain('组件已在别处被修改')
    expect(stale).toContain('当前草稿不会丢失')
    expect(stale).toContain('加载最新版本')

    const inUse = render(createElement(ComponentRejectionAlert, {
      rejection: { kind: 'in_use', detail: '组件 banned-words 正被 Skill「长篇续写」引用，不能修改标识', usages: component.usages },
      onReload: noop,
    }))
    expect(text(inUse)).toContain('不能修改标识')
    expect(inUse).toContain('href="/skills?skill=5"')
    expect(inUse).toContain('aria-label="组件引用方"')

    const breaks = render(createElement(ComponentRejectionAlert, {
      rejection: { kind: 'breaks', failures: [{ key: 'chapter.generate', name: '写章节', errors: [{ part: 'user', line: 3, message: '组件 banned-words：未知变量：x' }] }] },
      onReload: noop,
    }))
    expect(text(breaks)).toContain('模板「写章节」')
    expect(text(breaks)).toContain('用户段第3行：组件banned-words：未知变量：x')
  })
})

describe('Skill 与模板覆盖编辑页的组件引用', () => {
  const skill = (over: Partial<Skill> = {}): Skill => ({
    ...skillFixture(), body: '正文{{> banned-words }}', references: [], components: [{ id: 1, slug: 'banned-words', name: '禁用词' }], ...over,
  })
  const props = { open: true, skillId: 1, scope: 'mine' as const, isAdmin: false, onClose: noop, onSaved: noop, onCopy: noop }

  it('我的 Skill 显示引用的组件（跳到组件库）和插入入口', () => {
    const html = render(createElement(SkillForm, { ...props, skill: skill() }))
    expect(html).toContain('href="/prompt-components?id=1"')
    expect(text(html)).toContain('引用的组件：禁用词')
    expect(html).toContain('aria-label="插入组件引用"')
  })

  it('全局 Skill 提示不能引用组件，不提供插入入口', () => {
    auth.superAdmin = true
    try {
      const html = render(createElement(SkillForm, { ...props, scope: 'global', isAdmin: true, skillId: null, skill: null }))
      expect(text(html)).toContain('全局Skill不能引用提示词组件')
      expect(html).not.toContain('aria-label="插入组件引用"')
    } finally {
      auth.superAdmin = false
    }
    // 普通用户查看只读的全局 Skill：既不提示也不提供插入
    const readonlyGlobal = render(createElement(SkillForm, { ...props, skill: skill({ scope: 'global', editable: false, components: [] }) }))
    expect(readonlyGlobal).not.toContain('aria-label="插入组件引用"')
    expect(text(readonlyGlobal)).not.toContain('全局Skill不能引用提示词组件')
  })

  it('模板覆盖编辑器显示已保存覆盖引用的组件与插入入口', () => {
    loader.data = {
      key: 'chapter.generate', name: '写章节', category: 'chapter', category_label: '章节', description: '', overridden: true, enabled: true,
      outdated: false, updated_at: '', default_system: '默认系统', default_user: '默认用户', default_fingerprint: 'fp', variables: [], locked: [],
      override: { system: '{{> banned-words }}', user: '用户', enabled: true, base_fingerprint: 'fp', base_system: '', base_user: '', updated_at: '' },
      components: [{ id: 1, slug: 'banned-words', name: '禁用词' }],
    } satisfies PromptTemplateDetail
    const html = render(createElement(PromptEditor, { templateKey: 'chapter.generate', onChanged: noop, onDirtyChange: noop }))
    expect(html).toContain('href="/prompt-components?id=1"')
    expect(html).toContain('aria-label="插入组件引用"')
  })
})
