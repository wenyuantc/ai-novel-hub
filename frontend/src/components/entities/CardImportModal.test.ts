import { type ModalProps } from 'antd'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import CardImportModal from './CardImportModal'

const state = vi.hoisted(() => ({
  role: 'viewer' as 'owner' | 'viewer' | 'editor' | 'admin',
  isCollaborator: true,
  canEdit: false,
  accessUncertain: false,
  data: null as unknown,
  hasPreview: true,
}))

vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual, Modal: (props: ModalProps) => createElement('section', null,
    createElement('h2', null, props.title), props.children) }
})
vi.mock('../../stores/auth', () => ({
  useAuthStore: (selector: (value: unknown) => unknown) => selector({ user: { id: 1, is_super_admin: false, effective_permissions: [] } }),
}))
vi.mock('../../stores/project', () => ({
  useProjectStore: (selector: (value: unknown) => unknown) => selector({ scopeVersion: 1 }),
}))
vi.mock('../../hooks/useProjectAccess', () => ({
  useProjectAccess: () => ({ ...state, canRead: true, canReadNow: () => true, canEditNow: () => state.canEdit }),
}))
vi.mock('./useCardImport', () => ({
  useCardImport: () => ({
    state: { loading: false, importing: false, result: null, error: state.accessUncertain ? '作品权限需要确认' : null, reviewRequired: false,
      accessUncertain: state.accessUncertain, checkingAccess: false, data: state.data,
      reviewPreview: null, needsPreview: false, fileName: '卡片.json', metadata: { version: 3, entries: [], external: [], usages: [] },
      preview: state.hasPreview ? { fields_revision: 0, categories_revision: 0, ai_visibility_revision: 0, can_import: true,
        field_definitions: [], references: [], characters: [], organizations: [], careers: [], relationships: [], warnings: [] } : null,
      choices: { characters: {}, organizations: {}, careers: {} }, mappings: { references: {}, definitions: {} },
      selectedDefinitions: {}, selectedEntries: {} },
    controller: { selectFile: vi.fn(), refreshPreview: vi.fn(), confirm: vi.fn(), setResolution: vi.fn() },
  }),
}))

beforeEach(() => {
  state.role = 'viewer'
  state.isCollaborator = true
  state.canEdit = false
  state.accessUncertain = false
  state.data = null
  state.hasPreview = true
})

function render() {
  return renderToStaticMarkup(createElement(CardImportModal, { projectId: 1, open: true, onClose: () => {}, onImported: () => {} }))
}

describe('卡片预览界面', () => {
  it('查看者可查看预览，但不挂载导入确认', () => {
    const html = render()
    expect(html).toContain('预览卡片')
    expect(html).toContain('当前可以预览卡片')
    expect(html).toContain('更新预览')
    expect(html).not.toContain('确认导入')
  })

  it('编辑者或有管理写权限的查看者可以确认，纯管理员不能打开', () => {
    state.canEdit = true
    expect(render()).toContain('确认导入')
    state.role = 'admin'
    state.isCollaborator = false
    expect(render()).toBe('')
  })

  it('权限未确认时提供重查入口并禁用导入；首次预览失败后可手动读取', () => {
    state.canEdit = true
    state.accessUncertain = true
    expect(render()).toContain('重新确认作品权限')
    expect(render()).toMatch(/disabled[^>]*>[^<]*<span>确认导入/)
    state.accessUncertain = false
    state.data = { format: 'ainovel.cards' }
    state.hasPreview = false
    expect(render()).toContain('重新读取卡片预览')
  })
})
