import { App } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { projectApi } from '../../api/projects'
import type { IndexStatus, KbDocument, Project, ProjectAccess, User } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import KnowledgePanel from './KnowledgePanel'

// E6-e：知识库页的协作者视图以服务端 index-status 为准（检索模式 / 降级原因），作者界面保持原样。
const harness = vi.hoisted(() => ({
  status: undefined as IndexStatus | undefined,
  docs: [] as unknown[],
  loaderCalls: 0,
  indexOptions: undefined as Record<string, unknown> | undefined,
}))
vi.mock('../../stores/auth', async (original) => {
  const actual = await original<typeof import('../../stores/auth')>()
  const hook = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { ...actual, useAuthStore: Object.assign(hook, actual.useAuthStore) }
})
vi.mock('../../stores/project', async (original) => {
  const actual = await original<typeof import('../../stores/project')>()
  const hook = (selector: (state: ReturnType<typeof actual.useProjectStore.getState>) => unknown) => selector(actual.useProjectStore.getState())
  return { ...actual, useProjectStore: Object.assign(hook, actual.useProjectStore) }
})
vi.mock('../../stores/tasks', () => ({ useTasksStore: (selector: (state: { track: () => void }) => unknown) => selector({ track: () => {} }) }))
vi.mock('../../hooks/useProjectId', () => ({ useProjectId: () => 2 }))
// 第一次调用是文档列表，第二次是检索设置（设置留空，避免无关提示）
vi.mock('../../hooks/useLoader', () => ({
  useLoader: () => ({ data: harness.loaderCalls++ % 2 === 0 ? harness.docs : undefined, loading: false, error: null, setData: () => {} }),
}))
vi.mock('../../hooks/useIndexStatus', () => ({
  useIndexStatus: (_pid: number, options: Record<string, unknown>) => {
    harness.indexOptions = options
    return { status: options.enabled ? harness.status : undefined, refresh: () => {}, version: 0 }
  },
}))
vi.mock('../../components/kb/KbChunksDrawer', () => ({ default: () => null }))
vi.mock('../../components/kb/RetrievalSettingsCard', () => ({ default: () => null }))
vi.mock('../../components/mentions/SourceMentionFocus', () => ({ default: () => null }))

const editorUser: User = { id: 1, username: '编辑者', created_at: '', is_admin: false, is_super_admin: false,
  effective_permissions: [], roles: [], authorization_revision: 1 }
const ownerUser: User = { ...editorUser, id: 9, username: '作者' }
const editor: ProjectAccess = { role: 'editor', is_collaborator: true, authorization_version: 'grant-1',
  allow_owner_ai: true, can_read: true, can_edit_content: true, can_use_ai: true, can_export: true,
  can_manage_collaborators: false, can_recycle: false, can_backup: false, can_write_reading_progress: false }
const viewer: ProjectAccess = { ...editor, role: 'viewer', can_edit_content: false, can_use_ai: false }
const owner: ProjectAccess = { ...editor, role: 'owner', is_collaborator: false, authorization_version: null,
  can_manage_collaborators: true, can_recycle: true, can_backup: true, can_write_reading_progress: true }
function project(access: ProjectAccess): Project {
  return { id: 2, owner: { id: 9, username: '作者' }, access, title: '作品', genre: '', premise: '', status: 'planning',
    created_at: '', updated_at: '', chapter_count: 0, total_words: 0, cover_updated_at: null, auto_analyze: true,
    wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null, default_style: null,
    synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null, last_edited_chapter_id: null, reading: null }
}
const doc: KbDocument = { id: 5, project_id: 2, filename: '资料.md', mime: 'text/markdown', size: 4, sha256: 'sha',
  status: 'ready_keyword_only', error: '', warnings: [], char_count: 4, chunk_count: 1, inject: true, created_at: '', updated_at: '' }
const vectorStatus: IndexStatus = { embedding_model: null, needs_rebuild: true, stale_count: 3, stale: { kb: 2, memory: 1 },
  indexing: false, owner_embedding_configured: true, retrieval_mode: 'vector', keyword_reason: null }

async function render(access: ProjectAccess, user: User, status: IndexStatus | undefined) {
  useAuthStore.setState({ status: 'authenticated', user })
  vi.spyOn(projectApi, 'get').mockResolvedValue(project(access))
  await useProjectStore.getState().load(2)
  harness.status = status
  harness.loaderCalls = 0
  return renderToString(createElement(MemoryRouter, null, createElement(App, null, createElement(KnowledgePanel))))
}

beforeEach(() => {
  useProjectStore.getState().reset()
  harness.docs = [doc]
  harness.indexOptions = undefined
})
afterEach(() => { useProjectStore.getState().reset(); vi.restoreAllMocks() })

describe('知识库页协作者视图', () => {
  it('授权编辑者且作者向量可用：费用提示、检索调试、文档重建与全书重建（经费用确认）、待建向量数', async () => {
    const html = await render(editor, editorUser, vectorStatus)
    expect(html).toContain('使用作者共享的向量模型，费用由作者承担')
    expect(html).toContain('3 条资料待建向量')
    expect(html).toContain('<span>全书重建</span>')
    expect(html).toContain('检索调试')
    expect(html).toContain('查询会使用作者共享的向量模型，费用由作者承担。')
    expect(html).toMatch(/<button[^>]*type="button"[^>]*class="[^"]*ant-btn-link[^"]*"(?![^>]*disabled)[^>]*><span>重建索引<\/span>/)
    // 编辑者视图不能出现作者端的「去模型配置」或「未配置向量模型」
    expect(html).not.toContain('去模型配置')
    expect(html).not.toContain('未配置向量模型')
    // 进度改为前台可见轮询（kb.index 订阅对协作者为 404）
    expect(harness.indexOptions).toMatchObject({ enabled: true, foreground: true })
  })

  it.each([
    ['owner_embedding_not_allowed', '作者未授权使用其模型配置', false],
    ['embedding_config_missing', '作者未配置向量模型', true],
    ['owner_embedding_unavailable', '作者的向量模型配置当前不可用', true],
  ] as const)('关键词降级 %s：仅关键词并说明原因；重建置灰；未授权时隐藏检索调试', async (reason, text, debug) => {
    const html = await render({ ...editor, allow_owner_ai: reason !== 'owner_embedding_not_allowed' }, editorUser,
      { ...vectorStatus, retrieval_mode: 'keyword', keyword_reason: reason, owner_embedding_configured: reason !== 'embedding_config_missing' })
    expect(html).toContain('仅关键词检索')
    expect(html).toContain(text)
    expect(html).toContain('3 条资料待建向量')
    expect(html).not.toContain('<span>全书重建</span>')
    expect(html).toMatch(/<button[^>]*disabled[^>]*><span>重建索引<\/span>/)
    expect(html.includes('检索调试')).toBe(debug)
    expect(html).not.toContain('去模型配置')
    expect(html).toContain('点击或拖拽文件到这里上传')
  })

  it('状态未读到时不臆断能力：上传可用、重建置灰、不显示检索调试', async () => {
    const html = await render(editor, editorUser, undefined)
    expect(html).toContain('正在确认索引方式')
    expect(html).not.toContain('检索调试')
    expect(html).toMatch(/<button[^>]*disabled[^>]*><span>重建索引<\/span>/)
  })

  it('viewer 只读：不读索引状态、无上传 / 重建 / 检索调试', async () => {
    const html = await render(viewer, editorUser, vectorStatus)
    expect(harness.indexOptions).toMatchObject({ enabled: false })
    expect(html).not.toContain('点击或拖拽文件到这里上传')
    expect(html).not.toContain('重建索引')
    expect(html).not.toContain('<span>全书重建</span>')
    expect(html).not.toContain('检索调试')
    expect(html).not.toContain('待建向量')
  })

  it('作者界面保持原样：原横幅（带模型名）+ 原重建按钮，无协作者费用提示；仅多出待建向量数', async () => {
    const html = await render(owner, ownerUser, { ...vectorStatus, embedding_model: '作者向量 · bge-m3' })
    expect(html).toContain('需要重建索引：3 条资料待建向量')
    expect(html).toContain('作者向量 · bge-m3')
    expect(html).toContain('检索调试')
    expect(html).not.toContain('费用由作者承担')
    expect(html).not.toContain('<span>全书重建</span>')
    expect(harness.indexOptions).toMatchObject({ enabled: true, foreground: false })
  })

  it('作者本人向量配置不可用（裁决 18）：提示去模型配置检查，横幅与文档重建置灰并说明原因', async () => {
    const html = await render(owner, ownerUser, { ...vectorStatus, embedding_model: '作者向量 · bge-m3',
      retrieval_mode: 'keyword', keyword_reason: 'owner_embedding_unavailable' })
    expect(html).toContain('向量配置不可用，请到模型配置检查')
    expect(html).toContain('去模型配置')
    expect(html).not.toContain('需要重建索引')
    // 横幅与文档操作里的「重建索引」都置灰，不存在可点击的重建按钮
    expect(html).not.toMatch(/<button(?![^>]*disabled)[^>]*><span>重建索引<\/span>/)
    expect(html.match(/<button[^>]*disabled[^>]*><span>重建索引<\/span>/g)?.length).toBe(2)
    expect(html).not.toContain('费用由作者承担')
  })
})
