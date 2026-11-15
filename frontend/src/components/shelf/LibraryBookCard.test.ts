import type { DropdownProps, MenuProps } from 'antd'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Project, ProjectAccess } from '../../api/types'
import LibraryBookCard from './LibraryBookCard'

const state = vi.hoisted(() => ({ menu: null as MenuProps | null }))
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual, Dropdown: (props: DropdownProps) => { state.menu = props.menu ?? null; return createElement('div', null, props.children) } }
})

const viewer: ProjectAccess = {
  role: 'viewer', is_collaborator: true, authorization_version: 'grant-1', allow_owner_ai: false,
  can_read: true, can_edit_content: false,
  can_export: true, can_use_ai: false, can_manage_collaborators: false, can_recycle: false, can_backup: false,
  can_write_reading_progress: false,
}
const project: Project = {
  id: 10, access: viewer, owner: { id: 1, username: '原作者' }, title: '山海', genre: '', premise: '',
  status: 'writing', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  chapter_count: 1, total_words: 20, cover_updated_at: null, auto_analyze: true,
  wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null, default_style: null,
  synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null,
  last_edited_chapter_id: 11, reading: null,
}
const render = (item: Project) => renderToStaticMarkup(createElement(StaticRouter, { location: '/projects' },
  createElement(LibraryBookCard, { project: item, onExport: vi.fn(), onBackup: vi.fn(), onDelete: vi.fn() })))
const keys = () => (state.menu?.items ?? []).flatMap((item) => item && 'key' in item && item.key ? [item.key] : [])

beforeEach(() => { state.menu = null })

describe('共享书架卡片', () => {
  it('查看者显示真实作者、角色、只读入口与导出，不显示创作、备份或删除', () => {
    const html = render(project)
    expect(html).toContain('原作者')
    expect(html).toContain('查看者')
    expect(html).toContain('作者模型未授权')
    expect(html).toContain('阅读作品')
    expect(html).not.toContain('继续创作')
    expect(keys()).toEqual(['read', 'overview', 'export'])
  })

  it('管理员兼查看者按实际能力显示人工创作和删除，不提供作者备份', () => {
    const html = render({ ...project, access: { ...viewer, can_edit_content: true, can_recycle: true } })
    expect(html).toContain('查看者')
    expect(html).toContain('继续创作')
    expect(keys()).toEqual(['read', 'overview', 'export', 'delete'])
  })

  it('缺少作者姓名时不把当前用户伪称为作者', () => {
    const html = render({ ...project, owner: null })
    expect(html).not.toContain('作者：')
    expect(html).not.toContain('原作者')
  })

  it('无有效读取能力的快照不渲染作品内容或操作', () => {
    expect(render({ ...project, access: { ...viewer, can_read: false } })).toBe('')
    expect(state.menu).toBeNull()
    expect(render({ ...project, access: { ...viewer, authorization_version: undefined } as unknown as ProjectAccess })).toBe('')
    expect(state.menu).toBeNull()
  })
})
