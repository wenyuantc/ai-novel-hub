import { App, type ModalProps } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import CoverGenerateModal from './CoverGenerateModal'

const view = vi.hoisted(() => ({ warning: null as string | null }))
vi.mock('../../hooks/useLoader', () => ({ useLoader: () => ({
  data: { prompt: '项目已有信息生成的提示词', warning: view.warning }, loading: false, error: null, reload: () => {},
}) }))
vi.mock('../AIConfigSelect', () => ({ default: () => null }))
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual, Modal: (props: ModalProps) => createElement('section', null, props.children) }
})

afterEach(() => { view.warning = null })

describe('封面提示词的私人风格告知', () => {
  it('后端未能继承私有风格时显示安全提示，并保留可编辑的提示词', () => {
    view.warning = '当前账号无法继承作品默认风格，已按无风格生成提示词。'
    const html = renderToString(createElement(App, null, createElement(CoverGenerateModal, {
      projectId: 9, title: '星河', onClose: () => {}, onSaved: () => {},
    })))
    expect(html).toContain(view.warning)
    expect(html).toContain('项目已有信息生成的提示词')
  })

  it('可正常继承风格时不显示该提示', () => {
    const html = renderToString(createElement(App, null, createElement(CoverGenerateModal, {
      projectId: 9, title: '星河', onClose: () => {}, onSaved: () => {},
    })))
    expect(html).not.toContain('无法继承作品默认风格')
    expect(html).toContain('项目已有信息生成的提示词')
  })
})
