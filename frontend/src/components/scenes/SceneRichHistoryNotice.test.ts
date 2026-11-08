import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { SceneRestorePreview } from '../../api/types'
import SceneRichHistoryNotice from './SceneRichHistoryNotice'
import { mayApplyRichHistory } from './sceneRichHistory'

const preview: SceneRestorePreview = {
  version_id: 3, has_scene_snapshot: true, content_revision: 7, scene_ids: [9], retiring_scene_ids: [], blockers: [],
  requires_rich_confirmation: true,
  rich_changes: [{ scene_id: 9, will_retire: false, before_sections: 3, after_sections: 1, before_markers: 2,
    after_markers: 0, sections_changed: true, markers_changed: true, format_changed: true,
    before_format_ranges: 4, after_format_ranges: 1 }],
}

describe('富文档历史对照', () => {
  it('明确呈现区块、标记和格式变化且未经确认不能采纳', () => {
    const html = renderToStaticMarkup(createElement(SceneRichHistoryNotice, {
      preview, confirmed: false, disabled: false, onConfirm: () => undefined,
    }))
    expect(html).toContain('富文档资料将随历史变化')
    expect(html).toContain('场景 #9')
    expect(html).toContain('格式改变')
    expect(html).toContain('我已核对上述变化')
    expect(html).toContain('type="checkbox"')
    expect(mayApplyRichHistory(preview, false)).toBe(false)
    expect(mayApplyRichHistory(preview, true)).toBe(true)
  })

  it('无资料损失时不强制额外确认，重新预览可清空确认', () => {
    expect(mayApplyRichHistory({ ...preview, requires_rich_confirmation: false }, false)).toBe(true)
    expect(mayApplyRichHistory(preview, false)).toBe(false)
  })
})
