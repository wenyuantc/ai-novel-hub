import { App, type ButtonProps, type DropdownProps, type ModalProps, type PopconfirmProps } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { Route, Routes } from 'react-router-dom'
import { StaticRouter } from 'react-router-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Assignment, Career, Character } from '../../api/types'
import { assignmentApi, careerApi } from '../../api/careers'
import { memberApi } from '../../api/organizations'
import { chapterApi } from '../../api/chapters'
import ChaptersPanel from '../../pages/project/ChaptersPanel'
import CareerEditDrawer from '../careers/CareerEditDrawer'
import CharacterCareersTab from '../careers/CharacterCareersTab'
import CharacterMembershipsTab from '../entities/CharacterMembershipsTab'

const view = vi.hoisted(() => ({
  canEdit: false, data: [] as unknown[], buttons: [] as ButtonProps[],
  confirms: [] as PopconfirmProps[], modals: [] as ModalProps[], menus: [] as DropdownProps[], stageDisabled: undefined as boolean | undefined,
}))
vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => ({
  canEdit: view.canEdit, canEditNow: () => view.canEdit, canUseAI: false,
}) }))
vi.mock('../../hooks/useLoader', () => ({ useLoader: () => ({
  data: view.data.shift(), loading: false, reload: () => {},
}) }))
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => true, useModalWidth: () => 360 }))
vi.mock('../systemSettings/useSettingsConfirmation', () => ({ useSettingsConfirmation: () => (props: ModalProps) => { view.modals.push(props) } }))
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual,
    Drawer: ({ children }: { children?: import('react').ReactNode }) => createElement('section', null, children),
    Button: (props: ButtonProps) => { view.buttons.push(props); return createElement(actual.Button, props) },
    Popconfirm: (props: PopconfirmProps) => { view.confirms.push(props); return createElement(actual.Popconfirm, props) },
    Modal: (props: ModalProps) => { view.modals.push(props); return createElement(actual.Modal, props) },
    Dropdown: (props: DropdownProps) => { view.menus.push(props); return createElement(actual.Dropdown, props) },
  }
})
vi.mock('../careers/StageListEditor', async (original) => {
  const actual = await original<typeof import('../careers/StageListEditor')>()
  return { default: (props: Parameters<typeof actual.default>[0]) => {
    view.stageDisabled = props.disabled
    return createElement(actual.default, props)
  } }
})

const character: Character = { id: 3, project_id: 9, name: '甲', role_type: '', profile: '', relations_text: '', aliases: [], age: '', gender: '', appearance: '', personality: '', background: '', current_state: '', life_status: 'alive', always_in_context: false, sort: 0 }
const career: Career = { id: 4, project_id: 9, name: '剑修', kind: 'main', description: '', requirements: '', abilities: '', rules: '', sort: 0, stages: [{id: 5, position: 1, name: '练气', description: ''}], holder_count: 1, created_at: '', updated_at: '' }
const assignment: Assignment = { id: 6, character_id: 3, career_id: 4, career_name: '剑修', career_kind: 'main', role: 'main', stage_id: 5, stage_name: '练气', stage_position: 1, stage_total: 1, note: '', updated_at: '' }
const noop = () => {}
const button = (label: string) => {
  const found = view.buttons.find((props) => typeof props.children === 'string' && props.children.trim() === label)
  expect(found, label).toBeDefined()
  return found!
}
const render = (element: import('react').ReactElement) => renderToString(createElement(App, null, element))
afterEach(() => { vi.restoreAllMocks(); view.canEdit = false; view.data = []; view.buttons = []; view.confirms = []; view.modals = []; view.menus = []; view.stageDisabled = undefined })

describe('人工只读的关联子表单和真实提交守卫', () => {
  it.each([false, true])('职业阶段编辑器继承canEdit=%s，不能只禁用保存', async (canEdit) => {
    view.canEdit = canEdit
    view.data = [[]]
    const update = vi.spyOn(careerApi, 'update')
    render(createElement(CareerEditDrawer, {projectId: 9, open: true, career, onClose: noop, onSaved: noop}))
    expect(view.stageDisabled).toBe(!canEdit)
    expect(button('添加阶段').disabled).toBe(!canEdit)
    expect(button('保存').disabled).toBe(!canEdit)
    if (!canEdit) { await Reflect.apply(button('保存').onClick!, undefined, []); expect(update).not.toHaveBeenCalled() }
  })
  it('已有组织也不能向只读角色开启加入组织，过期保存回调不发请求', async () => {
    view.data = [[], [{ id: 8, name: '天剑宗' }]]
    const create = vi.spyOn(memberApi, 'create')
    render(createElement(CharacterMembershipsTab, {projectId: 9, character}))
    expect(button('加入组织').disabled).toBe(true)
    // 绕过禁用控件，直接执行实际组件保留的确认回调，写前守卫仍拒绝。
    await Reflect.apply(view.modals[0].onOk!, undefined, [])
    expect(create).not.toHaveBeenCalled()
  })
  it('只读职业卸下按钮禁用，旧确认回调也不能执行API', async () => {
    view.data = [[assignment], [], [career]]
    const remove = vi.spyOn(assignmentApi, 'remove')
    render(createElement(CharacterCareersTab, {projectId: 9, character}))
    expect(button('卸下').disabled).toBe(true)
    const confirmation = view.confirms.find((props) => props.title === '卸下该职业？')
    expect(confirmation).toBeDefined()
    await confirmation!.onConfirm?.()
    expect(remove).not.toHaveBeenCalled()
  })
  it('移动章节菜单明确显示查看并禁用删除，旧危险确认不能提交', async () => {
    view.data = [[{id: 7, project_id: 9, title: '第一章', content: '', chapter_no: 1, word_count: 1, status: 'draft', updated_at: ''}]]
    const remove = vi.spyOn(chapterApi, 'remove')
    render(createElement(StaticRouter, {location: '/projects/9/chapters'}, createElement(Routes, null,
      createElement(Route, {path: '/projects/:pid/chapters', element: createElement(ChaptersPanel)}),
    )))
    const menu = view.menus.find((props) => props.menu?.items?.some((item) => item && 'key' in item && item.key === 'delete'))?.menu
    expect(menu).toBeDefined()
    expect(menu!.items).toEqual(expect.arrayContaining([
      expect.objectContaining({key: 'edit', label: '查看'}),
      expect.objectContaining({key: 'delete', disabled: true}),
    ]))
    Reflect.apply(menu!.onClick!, undefined, [{key: 'delete'}])
    const confirmation = view.modals.find((props) => props.title === '删除该章节？')
    expect(confirmation).toBeDefined()
    await Reflect.apply(confirmation!.onOk!, undefined, [])
    expect(remove).not.toHaveBeenCalled()
  })
})
