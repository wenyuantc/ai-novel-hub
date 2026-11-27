import { Alert, Button, Input } from 'antd'
import { isValidElement, type ReactElement, type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../../api/client'
import { dramaApi, type Episode } from '../api'
import EpisodeText from './EpisodeText'
import type { Access } from './PageScope'

// 仅驱动失焦、409 和用户选择，不依赖浏览器的 Ant Design 输入法行为。
const state = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0 }))
vi.mock('react', async (original) => {
  const actual = await original<typeof import('react')>()
  return {
    ...actual,
    useState: <T>(initial: T) => {
      const index = state.cursor++
      if (!(index in state.slots)) state.slots[index] = initial
      return [
        state.slots[index] as T,
        (value: T) => {
          state.slots[index] = value
        },
      ]
    },
    useRef: <T>(initial: T) => {
      const index = state.cursor++
      return (state.slots[index] ??= { current: initial })
    },
  }
})
function elements(node: ReactNode): ReactElement<Record<string, unknown>>[] {
  if (Array.isArray(node)) return node.flatMap(elements)
  if (!isValidElement<Record<string, unknown>>(node)) return []
  return [node, ...elements(node.props.children as ReactNode), ...elements(node.props.description as ReactNode)]
}
afterEach(() => {
  vi.restoreAllMocks()
  state.slots = []
})
describe('剧本文本保存冲突', () => {
  it('保留本地草稿和编辑时修订号，明确覆盖时再用最新修订号保存', async () => {
    const episode: Episode = {
      id: 2,
      series_id: 1,
      number: 1,
      title: '试播',
      source_text: '雨夜',
      script: '旧剧本',
      script_skipped: false,
      revision: 4,
      sources: [],
    }
    const access = { canEdit: true, canEditNow: () => true } as Access
    const props = { episode, field: 'script' as const, label: '剧本', access, updated: vi.fn(), onDirty: vi.fn() }
    const page = () => {
      state.cursor = 0
      const nodes = elements(EpisodeText(props))
      const input = nodes.find((n) => n.type === Input.TextArea)!
      return { nodes, input: input.props }
    }
    const patch = vi.spyOn(dramaApi, 'updateEpisode').mockRejectedValueOnce(new ApiError(409, '修订号过期'))
    const local = '工作台本地修改'
    const change = page().input.onChange as (e: { target: { value: string } }) => void
    change({ target: { value: local } })
    const blur = page().input.onBlur as () => void
    blur()
    await vi.waitFor(() => expect(page().nodes.some((n) => n.type === Alert)).toBe(true))
    expect(page().input.value).toBe(local)
    expect(patch).toHaveBeenCalledWith(2, { script: local, revision: 4 })
    expect(props.updated).not.toHaveBeenCalled()
    vi.spyOn(dramaApi, 'episode').mockResolvedValue({ ...episode, revision: 7, script: '另一位编辑刚保存的内容' })
    patch.mockResolvedValue({ ...episode, revision: 8, script: local })
    const confirm = page().nodes.find((n) => n.type === Button && n.props.children === '覆盖保存')!.props
      .onClick as () => void
    confirm()
    await vi.waitFor(() => expect(props.updated).toHaveBeenCalledOnce())
    expect(patch).toHaveBeenLastCalledWith(2, { script: local, revision: 7 })
  })
})
