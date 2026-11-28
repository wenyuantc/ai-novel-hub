import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import EpisodeModelSettings from './EpisodeModelSettings'

vi.mock('../../../components/AIConfigSelect', () => ({
  default: (props: { id: string; modelType: string; value?: number; disabled: boolean }) =>
    createElement('select', {
      id: props.id,
      'data-model-type': props.modelType,
      'data-selected': props.value,
      disabled: props.disabled,
    }),
}))
it('本集顶栏显示三种独立的模型，并在保存时锁住选择', () => {
  const html = renderToStaticMarkup(
    createElement(EpisodeModelSettings, {
      settings: {
        values: { text: 1, image: 2, video: 3 },
        ready: false,
        saving: true,
        unavailable: [],
        error: '',
        reload: vi.fn(),
        change: vi.fn(),
        binding: { ready: false, unavailable: [], configFor: () => undefined, reload: vi.fn() },
      },
    }),
  )
  for (const [type, label, id] of [
    ['text', '文本模型', 1],
    ['image', '图片模型', 2],
    ['video', '视频模型', 3],
  ]) {
    expect(html).toContain(`for="drama-model-${type}">${label}</label>`)
    expect(html).toContain(`data-model-type="${type}" data-selected="${id}" disabled`)
  }
  expect(html).toContain('正在保存模型选择')
})

it('型号目录未确认时阻止该类型调用，可由用户明确清除', async () => {
  const { confirmEpisodeModel } = await import('../useEpisodeModelSettings')
  const prefs = { text: 11, image: 22, video: null }
  expect(confirmEpisodeModel(prefs, 'text', [])).toBeUndefined()
  expect(confirmEpisodeModel(prefs, 'video', [])).toBeNull()
  const html = renderToStaticMarkup(
    createElement(EpisodeModelSettings, {
      settings: {
        values: prefs,
        ready: true,
        saving: false,
        unavailable: ['text'],
        error: '',
        reload: vi.fn(),
        change: vi.fn(),
        binding: { ready: true, unavailable: ['text'], configFor: () => undefined, reload: vi.fn() },
      },
    }),
  )
  expect(html).toContain('当前无法确认已保存的配置')
  expect(html).toContain('清除选择')
  expect(html).not.toContain('data-model-type="text"')
})
