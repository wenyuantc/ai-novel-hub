import { createElement, type ReactNode } from 'react'
import { renderToString } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { Career, SettingThumbnailInfo } from '../../api/types'
import type { SettingAiSession } from '../settingAi/settingAiController'
import type { SettingTagsSession } from '../settingTags/settingTagsController'
import CareerEditDrawer from './CareerEditDrawer'

const harness = vi.hoisted(() => ({
  save: null as (() => void) | null,
  changeImage: null as ((value: SettingThumbnailInfo | null) => void) | null,
  displayedImage: null as SettingThumbnailInfo | null,
  update: vi.fn(),
}))

vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => ({ canEdit: true, canEditNow: () => true }) }))
vi.mock('../../hooks/useLoader', () => ({ useLoader: () => ({ data: [], loading: false }) }))
vi.mock('../../api/careers', async (original) => {
  const actual = await original<typeof import('../../api/careers')>()
  return { ...actual, careerApi: { ...actual.careerApi, update: harness.update } }
})
vi.mock('../entities/SettingThumbnail', () => ({
  ProjectThumbnailEditor: (props: { thumbnail: SettingThumbnailInfo | null; onChange: typeof harness.changeImage }) => {
    harness.changeImage = props.onChange
    harness.displayedImage = props.thumbnail
    return null
  },
  ThumbnailNewHint: () => null,
}))
vi.mock('./StageListEditor', () => ({ default: () => null }))
vi.mock('../settingFields/SettingEntryFieldsPanel', () => ({ default: () => null }))
vi.mock('../settingAi/SettingAiDetailSections', () => ({ default: () => null }))
vi.mock('../settingTags/SettingTagsPanel', () => ({ default: () => null }))
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  const FakeForm = Object.assign(({ children }: { children: ReactNode }) => createElement('div', null, children), {
    Item: ({ children }: { children: ReactNode }) => createElement('div', null, children),
    useForm: () => [{ validateFields: async () => ({ name: '新名称', kind: 'main', description: '',
      requirements: '', abilities: '', rules: '' }) }],
  })
  return {
    ...actual,
    App: { useApp: () => ({ message: { success: vi.fn(), warning: vi.fn(), error: vi.fn() } }) },
    Drawer: ({ open, children }: { open: boolean; children: ReactNode }) => open ? createElement('div', null, children) : null,
    Form: FakeForm,
    Button: ({ children, onClick }: { children: ReactNode; onClick?: () => void }) => {
      if (children === '保存') harness.save = onClick ?? null
      return createElement('button', null, children)
    },
  }
})

const oldImage = { version: 'old', mime_type: 'image/webp', width: 120, height: 80 }
const newImage = { version: 'new', mime_type: 'image/webp', width: 120, height: 80 }
const career: Career = {
  id: 1, project_id: 4, name: '旧名称', kind: 'main', description: '', requirements: '', abilities: '', rules: '',
  sort: 1, stages: [{ id: 3, position: 1, name: '一阶', description: '' }], holder_count: 0,
  created_at: '2026-01-01', updated_at: '2026-01-01', thumbnail: oldImage,
}

describe('职业图片与文字保存的父子状态', () => {
  it('文字响应迟到时把最新图片一起返回父层，重新打开仍展示最新图片', async () => {
    let resolve!: (value: Career) => void
    harness.update.mockReturnValueOnce(new Promise<Career>((done) => { resolve = done }))
    const onSaved = vi.fn()
    const props = { projectId: 4, open: true, career, onClose: vi.fn(), onSaved,
      tagsSession: {} as SettingTagsSession, aiSession: {} as SettingAiSession }
    renderToString(createElement(CareerEditDrawer, props))
    expect(harness.save).not.toBeNull()
    harness.save?.()
    await vi.waitFor(() => expect(harness.update).toHaveBeenCalledOnce())
    harness.changeImage?.(newImage)
    resolve({ ...career, name: '新名称', thumbnail: oldImage })
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    const parentValue = onSaved.mock.calls[0][0] as Career
    expect(parentValue.name).toBe('新名称')
    expect(parentValue.thumbnail).toEqual(newImage)
    renderToString(createElement(CareerEditDrawer, { ...props, career: parentValue }))
    expect(harness.displayedImage).toEqual(newImage)
  })
})
