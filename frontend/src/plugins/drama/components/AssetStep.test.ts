import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import AssetStep from './AssetStep'

vi.mock('../../../hooks/useLoader', () => ({
  useLoader: () => ({
    data: {
      characters: [
        {
          id: 1,
          name: '小云',
          novel_character_name: '云生',
          looks: [{ id: 2, name: '常服', is_default: true, image_prompt: '青衫角色立绘' }],
        },
      ],
      scenes: [{ id: 3, location: '花园', time_of_day: '黄昏', novel_world_setting_title: '南园' }],
      props: [{ id: 4, name: '药瓶' }],
    },
  }),
}))

it('查看者可看关联设定、默认造型和提示词，所有写入与 AI 操作隐藏', () => {
  const html = renderToStaticMarkup(
    createElement(AssetStep, { pid: 1, sid: 2, eid: 3, canEdit: false, canEditNow: () => false, onTask: vi.fn() }),
  )
  for (const text of ['已关联小说角色', '云生', '已关联小说地点', '南园', '默认造型', '青衫角色立绘'])
    expect(html).toContain(text)
  for (const text of ['从剧本提取资产', '生成图片提示词', '编辑角色', '新建道具', '移出本集'])
    expect(html).not.toContain(text)
})

it('集内可移出素材，剧级才允许删除，剧集和素材库均可生成提示词', () => {
  const episode = renderToStaticMarkup(
    createElement(AssetStep, {
      pid: 1,
      sid: 2,
      eid: 3,
      episodeRevision: 0,
      canEdit: true,
      canEditNow: () => true,
      onTask: vi.fn(),
    }),
  )
  const library = renderToStaticMarkup(
    createElement(AssetStep, { pid: 1, sid: 2, canEdit: true, canEditNow: () => true, onTask: vi.fn() }),
  )
  for (const text of ['移出本集', '从素材库加入本集', '从剧本提取资产']) {
    expect(episode).toContain(text)
    expect(library).not.toContain(text)
  }
  for (const html of [episode, library]) expect(html).toContain('生成图片提示词')
})
