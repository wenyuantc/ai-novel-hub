import { App } from 'antd'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AIConfig, Chapter, ProjectAccess, Scene } from '../../api/types'
import ChapterEditorPage from './ChapterEditorPage'

// 页面级：不 mock useHasAIConfig / useAIModelSource，按真实路由 pid、作品权限快照与本人配置推导按钮可用性。
const view = vi.hoisted(() => ({
  access: null as ProjectAccess | null,
  configs: [] as AIConfig[],
  drawer: null as { analyzeDisabled: boolean; reviseBlockedReason: string } | null,
  chapter: null as Chapter | null,
  scene: null as Scene | null,
}))
vi.mock('../../hooks/useProjectId', () => ({ useProjectId: () => 1 }))
vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => ({ canUseAI: true, canEdit: true, canEditNow: () => true, canReadNow: () => true, managingOther: false }) }))
vi.mock('../../hooks/useLoader', () => {
  let calls = 0
  return { useLoader: () => ({ data: calls++ % 2 === 0 ? view.chapter : [view.chapter], reload: () => {} }) }
})
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../../hooks/useElementWidth', () => ({ useElementWidth: () => [{ current: null }, 1000] }))
vi.mock('../../hooks/useAgentPageContext', () => ({ usePublishAgentContext: () => {} }))
vi.mock('../../hooks/useProjectRefresh', () => ({ useProjectRefresh: () => {} }))
vi.mock('../../hooks/useChapterAnalysis', () => ({ useChapterAnalysis: () => ({ state: null, latest: null, status: 'none', running: false, refresh: async () => null, start: async () => {} }) }))
vi.mock('../../hooks/useTaskStream', () => ({ useTaskStream: () => ({ running: false, task: null, text: '', error: null, follow: vi.fn(), detach: () => {} }) }))
vi.mock('../../stores/project', () => ({ useProjectStore: (select: (state: unknown) => unknown) => select({
  project: { id: 1, owner: { id: view.access?.role === 'owner' ? 1 : 2, username: '作者' }, access: view.access, auto_analyze: true, default_style: null },
  refresh: async () => {},
}) }))
vi.mock('../../stores/aiConfigs', () => ({ useAIConfigStore: (select: (state: unknown) => unknown) => select({ configs: view.configs, loaded: true }) }))
vi.mock('../../stores/tasks', () => ({ useTasksStore: (select: (state: unknown) => unknown) => select({ active: [], track: () => {} }) }))
vi.mock('../../stores/auth', () => ({ useAuthStore: (select: (state: unknown) => unknown) => select({ status: 'authenticated', user: { id: 1, is_super_admin: false, effective_permissions: [] } }) }))
vi.mock('react-router-dom', () => ({ useParams: () => ({ pid: '1', cid: '3' }), useNavigate: () => () => {}, useLocation: () => ({ state: null }), useSearchParams: () => [new URLSearchParams(), () => {}], useBlocker: () => ({ state: 'unblocked' }) }))
vi.mock('../../components/scenes/useSceneEditor', () => ({ useSceneEditor: () => ({
  scene: view.scene, draft: { title: '场景', content: '原场景正文', summary: '' }, dirty: false, saving: false, conflict: null, externalChange: false,
  page: { items: [{ id: 22 }], total: 1 }, controller: { getSnapshot: () => ({ scene: view.scene, draft: { content: '原场景正文' } }), load: async () => true, change: () => {} },
}) }))
vi.mock('../../components/chapter/AnalysisDrawer', () => ({ default: (props: { analyzeDisabled: boolean; reviseBlockedReason: string }) => { view.drawer = props; return null } }))
vi.mock('../../components/scenes/SceneWritingPanel', () => ({ default: () => null }))
vi.mock('../../components/chapter/VersionDrawer', () => ({ default: () => null }))
vi.mock('../../components/notes/NoteSidePanel', () => ({ default: () => null }))
vi.mock('../../components/chapter/RewritePanel', () => ({ default: () => null }))
vi.mock('../../components/chapter/CastSelect', () => ({ default: () => null }))
vi.mock('../../components/chapter/ChapterStyleSetting', () => ({ default: () => null }))
vi.mock('../../components/chapter/ContextPreviewPanel', () => ({ default: () => null }))
vi.mock('../../components/foreshadows/ChapterForeshadowButton', () => ({ default: () => null }))
vi.mock('../../components/AIConfigSelect', () => ({ default: () => null }))
vi.mock('../../components/skills/SkillSelect', () => ({ default: () => null }))
vi.mock('../../components/styles/StyleChoiceSelect', () => ({ default: () => null }))

const none: ProjectAccess = {
  role: null, is_collaborator: false, authorization_version: null, allow_owner_ai: false, can_read: false, can_edit_content: false,
  can_export: false, can_use_ai: false, can_manage_collaborators: false, can_recycle: false, can_backup: false,
  can_write_reading_progress: false,
}
const editor: ProjectAccess = { ...none, role: 'editor', is_collaborator: true, authorization_version: 'grant-1', can_read: true,
  can_export: true, can_edit_content: true, can_use_ai: true }
const owner: ProjectAccess = { ...none, role: 'owner', can_read: true, can_edit_content: true, can_export: true, can_use_ai: true,
  can_manage_collaborators: true, can_recycle: true, can_backup: true, can_write_reading_progress: true }
const textConfig = { id: 5, name: '本人文本', provider: 'openai', model: 'gpt', model_type: 'text', is_default: true } as unknown as AIConfig

function render(access: ProjectAccess, configs: AIConfig[]) {
  view.access = access
  view.configs = configs
  view.drawer = null
  const html = renderToStaticMarkup(createElement(App, null, createElement(ChapterEditorPage)))
  const button = html.match(/<button[^>]*>(?:(?!<\/button>).)*重新生成当前场景(?:(?!<\/button>).)*<\/button>/s)
  if (!button || !view.drawer) throw new Error('未渲染生成按钮或分析抽屉')
  const drawer: { analyzeDisabled: boolean; reviseBlockedReason: string } = view.drawer
  return { generateDisabled: /\sdisabled(=""|\s|>)/.test(button[0].replace(/<span.*$/s, '')), drawer }
}

beforeEach(() => {
  view.chapter = { id: 3, project_id: 1, outline_id: null, plan_id: null, style_id: null, chapter_no: 1, title: '章', status: 'draft', word_count: 10, content_revision: 1, content: '原整章正文', updated_at: '' }
  view.scene = {
    id: 22, chapter_id: 3, chapter_no: 1, chapter_title: '章', title: '场景22', sort: 0, content: '', summary: '',
    separator_before: '', active: true, word_count: 0, content_revision: 1, planning_revision: 1, created_at: '', updated_at: '',
    planning: { unit_id: null, binding_epoch: null, revision: 1, raw: { modes: { pov: 'inherit', tags: 'inherit', character: 'inherit', world: 'inherit', organization: 'inherit', career: 'inherit', relationship: 'inherit' }, pov_character_id: null, tag_ids: [], references: [] }, effective: { pov_character_id: null, pov_character_name: null, tags: [], references: [], sources: { pov: 'chapter', tags: 'chapter', character: 'chapter', world: 'chapter', organization: 'chapter', career: 'chapter', relationship: 'chapter' } } },
  } as unknown as Scene
})

describe('章节编辑页作者模型回退', () => {
  it('编辑者无本人模型且作者已授权：生成、分析与重写入口可用', () => {
    const result = render({ ...editor, allow_owner_ai: true }, [])
    expect(result.generateDisabled).toBe(false)
    expect(result.drawer.analyzeDisabled).toBe(false)
    expect(result.drawer.reviseBlockedReason).not.toBe('尚未配置 AI 模型')
  })

  it('编辑者未获授权或作者本人无配置：生成、分析与重写入口禁用', () => {
    for (const access of [editor, owner]) {
      const result = render(access, [])
      expect(result.generateDisabled).toBe(true)
      expect(result.drawer.analyzeDisabled).toBe(true)
      expect(result.drawer.reviseBlockedReason).toBe('尚未配置 AI 模型')
    }
  })

  it('有本人配置时按本人配置可用', () => {
    for (const access of [editor, owner]) {
      const result = render(access, [textConfig])
      expect(result.generateDisabled).toBe(false)
      expect(result.drawer.analyzeDisabled).toBe(false)
    }
  })
})
