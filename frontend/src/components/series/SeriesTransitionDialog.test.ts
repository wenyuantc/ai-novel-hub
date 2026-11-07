import { type ModalProps } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { SeriesImpact } from '../../api/series'
import SeriesTransitionDialog from './SeriesTransitionDialog'

const state = vi.hoisted(() => ({
  preview: { revision: 2, blockers: [], affected_books: [], source_stamp: null, requires_ai_settings: false } as SeriesImpact,
  modal: null as ModalProps | null,
  promote: vi.fn(),
  move: vi.fn(),
}))

vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual, Modal: (props: ModalProps) => { state.modal = props; return createElement('section', null, props.children) } }
})
vi.mock('../../hooks/useLoader', () => ({
  useLoader: () => ({ data: state.preview, loading: false, error: null, reload: vi.fn() }),
}))
vi.mock('../../api/series', () => ({ seriesApi: { promote: state.promote, move: state.move } }))
vi.mock('./useSeriesAction', () => ({
  useSeriesAction: () => ({ busy: false, error: null, clearError: vi.fn(),
    run: async (operation: () => Promise<unknown>, onSuccess: (value: unknown) => void) => onSuccess(await operation()),
  }),
}))

beforeEach(() => {
  state.modal = null
  state.move.mockReset()
  state.promote.mockReset()
})

describe('系列归属操作', () => {
  it('可恢复的回收站引用显示原因并阻止提交', () => {
    state.preview = { revision: 2, blockers: [{ project_id: 19, title: '前传', recycled: true, reasons: ['大纲快照'] }],
      affected_books: [], source_stamp: null, requires_ai_settings: false }
    const html = renderToString(createElement(SeriesTransitionDialog, {
      seriesId: 3, request: { action: 'move', entry_id: 34, project_id: 2 },
      label: '白塔', onClose: () => {}, onDone: () => {},
    }))
    expect(html.replaceAll('<!-- -->', '')).toContain('前传（回收站）')
    expect(html).toContain('大纲快照')
    expect(state.modal?.okButtonProps?.disabled).toBe(true)
    state.modal?.onOk?.({} as never)
    expect(state.move).not.toHaveBeenCalled()
  })

  it('提升使用预检版本、书内引用和明确的旧 AI 策略', () => {
    const source_stamp = 'f'.repeat(64)
    state.preview = { revision: 6, blockers: [], affected_books: [], source_stamp, requires_ai_settings: true }
    state.promote.mockResolvedValue({ revision: 7 })
    const html = renderToString(createElement(SeriesTransitionDialog, {
      seriesId: 3, request: { action: 'promote', entry_id: 37, project_id: 2, kind: 'character' },
      label: '陆鸣', onClose: () => {}, onDone: () => {},
    }))
    expect(html).toContain('原条目使用旧版 AI 规则')
    expect(state.modal?.okButtonProps?.disabled).toBe(false)
    state.modal?.onOk?.({} as never)
    expect(state.promote).toHaveBeenCalledWith(3, {
      project_id: 2, ref: { kind: 'character', id: 37 }, expected_revision: 6, source_stamp,
      ai_settings: { ai_mode: 'detected', mention_enabled: true },
    })
  })
})
