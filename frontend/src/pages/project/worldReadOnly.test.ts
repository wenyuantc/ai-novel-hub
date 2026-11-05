import { App, type AlertProps, type ModalProps, type TabsProps } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import WorldPanel from './WorldPanel'

const view = vi.hoisted(() => ({
  canEdit: false,
  status: 'confirmed' as 'confirmed' | 'legacy_unverified',
  alerts: [] as AlertProps[],
  modals: [] as ModalProps[],
}))
const item = { id: 3, project_id: 8, title: '青云山', category: 'place', content: '山门完好', sort: 1 }
vi.mock('../../hooks/useProjectId', () => ({ useProjectId: () => 8 }))
vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => ({
  canUseAI: false, canEdit: view.canEdit, canEditNow: () => view.canEdit, canReadNow: () => true,
}) }))
vi.mock('../../hooks/useLoader', () => ({ useLoader: () => ({ data: [item], loading: false, reload: () => {} }) }))
vi.mock('../../components/progressions/useWorldBaselineEditor', () => ({ useWorldBaselineEditor: () => ({
  editing: item, open: true, saving: false, baselineLoading: false, baselineError: '', baselineConflict: false,
  baseline: { baseline_status: view.status, baseline: { content: '山门完好' }, timeline_revision: 1 },
  openEditor: () => {}, closeEditor: () => {}, save: () => {}, reloadBaseline: () => {}, confirmBaseline: () => {}, progressionChanged: () => {},
}) }))
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual,
    Alert: (props: AlertProps) => { view.alerts.push(props); return createElement('div', null, props.message) },
    Modal: (props: ModalProps) => { view.modals.push(props); return createElement('section', null, props.children) },
    Tabs: (props: TabsProps) => createElement('div', null, props.items?.find((item) => item.key === props.activeKey)?.children),
  }
})

afterEach(() => { view.canEdit = false; view.status = 'confirmed'; view.alerts = []; view.modals = [] })
const renderWorld = () => renderToString(createElement(StaticRouter, { location: '/projects/8/world' }, createElement(App, null, createElement(WorldPanel))))

describe('世界观基础设定权限文案', () => {
  it('只读时显示查看语义，保存按钮仍禁用', () => {
    renderWorld()
    expect(view.modals[0].title).toBe('查看世界观条目')
    expect(view.modals[0].okButtonProps?.disabled).toBe(true)
    expect(view.alerts.find((item) => item.message === '正在查看基础设定')).toBeDefined()
    expect(view.alerts.find((item) => item.message === '正在编辑基础设定')).toBeUndefined()
  })

  it('可编辑时仍显示编辑语义，旧资料无论权限都提示待校准', () => {
    view.canEdit = true
    renderWorld()
    expect(view.alerts.find((item) => item.message === '正在编辑基础设定')).toBeDefined()
    view.alerts = []
    view.canEdit = false
    view.status = 'legacy_unverified'
    renderWorld()
    const warning = view.alerts.find((item) => item.message === '旧资料待校准')
    expect(warning?.description).toContain('早期设定可能不准确')
    expect(warning?.action).toBeUndefined()
  })
})
