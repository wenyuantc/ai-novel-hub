import { createElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import BackupModal from './BackupModal'
import type { BackupEstimate } from '../../api/types'

const state = vi.hoisted(() => ({ warnings: undefined as string[] | undefined }))

vi.mock('antd', async (original) => ({
  ...(await original<typeof import('antd')>()),
  Modal: ({ children }: {children: ReactNode}) => children,
}))
vi.mock('../../hooks/useLoader', () => ({
  useLoader: () => {
    const group = { rows: 0, chars: 0, bytes: 0 }
    const data: BackupEstimate = {
      core: group, versions: group, snapshots: group, generation_logs: group, inspiration: group,
      warnings: state.warnings,
    }
    return { data, loading: false, error: null }
  },
}))

it('显示当前作品的备份范围提示，旧版估算仍可显示', () => {
  function render(warnings?: string[]) {
    state.warnings = warnings
    return renderToStaticMarkup(createElement(BackupModal, { projectId: 1, onClose: () => {} }))
  }
  expect(render(['造型图片文件及图片历史'])).toContain('造型图片文件及图片历史')
  for (const warnings of [[], undefined]) {
    const html = render(warnings)
    expect(html).not.toContain('造型图片文件及图片历史')
    expect(html).toContain('章节版本')
  }
})
