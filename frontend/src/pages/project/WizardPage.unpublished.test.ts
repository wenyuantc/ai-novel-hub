import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import WizardPage from './WizardPage'

// 只捕获交给向导生成器的回调，验证「结果未发布」只刷新计数、不写入步骤完成进度。
const view = vi.hoisted(() => ({
  props: null as null | { onDone?: () => void; onUnpublished?: () => void },
  reload: vi.fn(),
  updateProgress: vi.fn(async () => ({ id: 1, wizard_progress: { done: ['world'], skipped: [], current: 'world' } })),
}))
vi.mock('../../components/WizardGenerator', () => ({
  default: (props: { onDone?: () => void; onUnpublished?: () => void }) => { view.props = props; return null },
}))
vi.mock('../../hooks/useLoader', () => ({ useLoader: () => ({ data: { world: 0, careers: 0, characters: 0, outline: 0, chapters: 0 }, loading: false, reload: view.reload }) }))
vi.mock('../../hooks/useProjectId', () => ({ useProjectId: () => 1 }))
vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => ({ canEditNow: () => true }) }))
vi.mock('../../api/projects', () => ({ projectApi: { updateWizardProgress: view.updateProgress } }))
vi.mock('../../stores/project', () => {
  const state = { project: { id: 1, wizard_progress: { done: [], skipped: [], current: 'world' } }, refresh: vi.fn(), setProject: vi.fn() }
  return {
    useProjectStore: Object.assign((select: (s: unknown) => unknown) => select(state), { getState: () => state }),
    captureProjectScope: () => ({ projectId: 1 }),
    isProjectScopeCurrent: () => true,
  }
})
vi.mock('react-router-dom', () => ({
  useNavigate: () => () => {}, useLocation: () => ({ key: 'k' }), useSearchParams: () => [new URLSearchParams()],
}))

beforeEach(() => {
  vi.clearAllMocks()
  view.props = null
})

describe('创作向导页：结果未发布', () => {
  it('只刷新设定计数，不把该步标记为完成', async () => {
    renderToStaticMarkup(createElement(WizardPage))
    expect(view.props?.onUnpublished).toBeTypeOf('function')
    view.props!.onUnpublished!()
    expect(view.reload).toHaveBeenCalledOnce()
    await Promise.resolve()
    expect(view.updateProgress).not.toHaveBeenCalled()
  })

  it('正常完成仍记录完成进度（对照）', async () => {
    renderToStaticMarkup(createElement(WizardPage))
    view.props!.onDone!()
    expect(view.reload).toHaveBeenCalledOnce()
    await vi.waitFor(() => expect(view.updateProgress).toHaveBeenCalledOnce())
    expect(view.updateProgress).toHaveBeenCalledWith(1, expect.objectContaining({ done: ['world'] }))
  })
})
