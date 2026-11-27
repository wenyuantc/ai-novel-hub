import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import ShotInspector from './ShotInspector'
import type { Shot } from '../shots'
import type { DramaAssets } from '../assetTypes'

const shot: Shot = {
  id: 6,
  episode_id: 8,
  position: 1,
  revision: 4,
  title: '渡口',
  description: '雨落江面。',
  shot_size: '远景',
  atmosphere: '阴冷',
  duration_seconds: 8,
  scene_id: 2,
  video_prompt: '穿过@码头。',
  look_ids: [],
  prop_ids: [],
}
const assets: DramaAssets = {
  characters: [],
  props: [],
  scenes: [
    {
      id: 2,
      revision: 4,
      location: '码头',
      time_of_day: '夜',
      description: '',
      lighting: '',
      image_prompt: '',
      novel_world_setting_id: null,
      novel_world_setting_title: null,
    },
  ],
}

describe('分镜检查器', () => {
  it('查看者可阅读保存的内容与已绑定素材标记，编辑器只读', () => {
    const html = renderToStaticMarkup(
      createElement(ShotInspector, {
        shot,
        assets,
        canEdit: false,
        canEditNow: () => false,
        onSaved: vi.fn(),
        onDirty: vi.fn(),
        onPrompt: vi.fn(),
      }),
    )
    expect(html).toContain('雨落江面。')
    expect(html).toContain('<mark>@码头</mark>')
    expect(html).toMatch(/<textarea[^>]*disabled/)
    expect(html).not.toContain('生成视频提示词')
    expect(html).not.toContain('保存分镜')
  })
  it('提供编辑保存和视频提示词入口', () => {
    const html = renderToStaticMarkup(
      createElement(ShotInspector, {
        shot,
        assets,
        canEdit: true,
        canEditNow: () => true,
        onSaved: vi.fn(),
        onDirty: vi.fn(),
        onPrompt: vi.fn(),
      }),
    )
    expect(html).toContain('保存分镜')
    expect(html).toContain('生成视频提示词')
  })
})
