import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import { invalidateRequestGeneration } from '../../api/client'
import { useSerializationAction } from './useSerializationAction'

const lifecycle = vi.hoisted(() => ({ unmount: undefined as (() => void) | undefined }))
vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(),
  useLayoutEffect: (setup: () => () => void) => { lifecycle.unmount = setup() },
}))
vi.mock('../../stores/auth', () => ({ useAuthStore: { getState: () => ({ user: { id: 4, created_at: 'today' } }) } }))

it.each(['账号切换', '权限收回', '抽屉关闭'])('%s后，迟到的用词核对结果不会显示', async (change) => {
  let canEdit = true
  let action!: ReturnType<typeof useSerializationAction>
  function Drawer() {
    action = useSerializationAction(() => canEdit)
    return null
  }
  renderToStaticMarkup(createElement(Drawer))
  let deliver!: (value: string) => void
  const response = new Promise<string>((resolve) => { deliver = resolve })
  const showHits = vi.fn()
  const pending = action.run(() => response, showHits)
  if (change === '账号切换') invalidateRequestGeneration()
  else if (change === '权限收回') canEdit = false
  else lifecycle.unmount?.()
  deliver('仅旧编辑者可见的命中')
  await pending
  expect(showHits).not.toHaveBeenCalled()
})
