import { describe, expect, it } from 'vitest'
import type { Project, ProjectAccess, Task } from '../api/types'
import {
  aiModelSource, canConfirmProjectDelete, COLLABORATOR_MEMORY_RETRY_HINT, projectAccess, showsTaskPanel, taskControlsAllowed,
  taskRetryBlockedHint,
  taskOutputMode,
} from './projectAccess'

const user = { id: 2, is_super_admin: false, effective_permissions: [], authorization_revision: 1 }
const other = { ...user, id: 3 }
const denied: ProjectAccess = {
  role: null, is_collaborator: false, authorization_version: null, allow_owner_ai: false, can_read: false, can_edit_content: false,
  can_export: false, can_use_ai: false, can_manage_collaborators: false, can_recycle: false,
  can_backup: false, can_write_reading_progress: false,
}
const project = (access: ProjectAccess) => ({ id: 8, owner: { id: 2, username: '作者' }, access })

const owner: ProjectAccess = {
  ...denied, role: 'owner', can_read: true, can_edit_content: true, can_export: true, can_use_ai: true,
  can_manage_collaborators: true, can_recycle: true, can_backup: true, can_write_reading_progress: true,
}
const viewer: ProjectAccess = { ...denied, role: 'viewer', is_collaborator: true, authorization_version: 'grant-1', can_read: true, can_export: true }
const editor: ProjectAccess = { ...viewer, role: 'editor', can_edit_content: true, can_use_ai: true, allow_owner_ai: true }
const admin: ProjectAccess = { ...denied, role: 'admin', can_read: true, can_edit_content: true, can_recycle: true }

describe('服务端作品能力', () => {
  it.each([
    ['作者', owner, user, true, true, true, true, true],
    ['查看者', viewer, other, true, false, false, false, false],
    ['编辑者', editor, other, true, true, false, true, false],
    ['管理员', admin, other, true, true, true, false, false],
    ['管理员兼查看者', { ...viewer, can_edit_content: true, can_recycle: true }, other, true, true, true, false, false],
    ['管理员兼编辑者', { ...editor, can_recycle: true }, other, true, true, true, true, false],
    ['无授权', denied, other, false, false, false, false, false],
  ])('%s 以每个服务端布尔值为准', (_name, snapshot, actor, read, edit, recycle, ai, ownerAI) => {
    const actual = projectAccess(project(snapshot as ProjectAccess), actor as typeof user, 8)
    // canUseAI 按服务端 can_use_ai（作者或 accepted 编辑者）；智能体/知识库等仅作者入口另用 canUseOwnerAI。
    expect(actual).toMatchObject({
      ready: true, canRead: read, canEdit: edit, canRecycle: recycle,
      canUseAI: ai, canUseAIEntitled: ai, canUseOwnerAI: ownerAI,
    })
    expect(actual.canExport).toBe((snapshot as ProjectAccess).can_export)
    expect(actual.canBackup).toBe((snapshot as ProjectAccess).can_backup)
    expect(actual.canManageCollaborators).toBe((snapshot as ProjectAccess).can_manage_collaborators)
    expect(actual.canWriteReadingProgress).toBe((snapshot as ProjectAccess).can_write_reading_progress)
  })

  it.each([
    ['作者', owner, user, true, true],
    ['授权编辑者', editor, other, true, true],
    ['未授权编辑者', { ...editor, allow_owner_ai: false }, other, true, true],
    ['查看者（只读本人会话）', viewer, other, false, true],
    ['管理员兼查看者（只读本人会话）', { ...viewer, can_edit_content: true, can_recycle: true }, other, false, true],
    ['管理员兼编辑者', { ...editor, can_recycle: true }, other, true, true],
    ['纯管理员（人工编辑不并入智能体）', admin, other, false, false],
    ['无授权', denied, other, false, false],
  ])('智能体能力：%s', (_name, snapshot, actor, use, view) => {
    const actual = projectAccess(project(snapshot as ProjectAccess), actor as typeof user, 8)
    expect(actual.canUseAgent).toBe(use)
    expect(actual.canViewAgent).toBe(view)
    expect(actual.authorizationVersion).toBe((snapshot as ProjectAccess).authorization_version)
  })

  it('智能体能力不由角色名推断：编辑者角色但服务端 can_use_ai=false 时只读', () => {
    const actual = projectAccess(project({ ...editor, can_use_ai: false }), other, 8)
    expect(actual).toMatchObject({ role: 'editor', canUseAgent: false, canViewAgent: true })
    expect(projectAccess(project({ ...owner, can_use_ai: false }), user, 8)).toMatchObject({ canUseAgent: false, canViewAgent: false })
    expect(projectAccess(project(editor), other, 9)).toMatchObject({ canUseAgent: false, canViewAgent: false, authorizationVersion: null })
  })

  it('缺失或不完整的服务端能力、无用户或不同项目一律失败关闭', () => {
    const missing = { id: 8, owner: null } as unknown as Pick<Project, 'id' | 'owner' | 'access'>
    const incomplete = project({ ...owner, can_edit_content: undefined } as unknown as ProjectAccess)
    const missingVersion = project({ ...viewer, authorization_version: undefined } as unknown as ProjectAccess)
    const emptyGrant = project({ ...viewer, authorization_version: '' })
    const unexpectedGrant = project({ ...owner, authorization_version: 'grant-1' })
    for (const actual of [projectAccess(missing, user, 8), projectAccess(incomplete, user, 8),
      projectAccess(missingVersion, other, 8), projectAccess(emptyGrant, other, 8), projectAccess(unexpectedGrant, user, 8),
      projectAccess(project(owner), null, 8), projectAccess(project(owner), user, 9)]) {
      expect(actual).toMatchObject({ ready: false, canRead: false, canEdit: false, canUseAI: false, canUseAIEntitled: false, canUseOwnerAI: false })
    }
  })

  it('角色只用于显示，不从角色推断人工/私有能力', () => {
    const snapshot = { ...viewer, can_edit_content: true, can_recycle: true }
    const actual = projectAccess(project(snapshot), other, 8)
    expect(actual).toMatchObject({ role: 'viewer', isCollaborator: true, canEdit: true, canRecycle: true,
      canUseAI: false, canBackup: false, canWriteReadingProgress: false })
  })

  it('身份角色与协作标记矛盾时不接受整份能力快照', () => {
    const contradictory = [
      { ...viewer, is_collaborator: false, authorization_version: null },
      { ...viewer, role: 'admin' as const },
      { ...viewer, role: null },
      { ...owner, is_collaborator: true, authorization_version: 'grant-1' },
    ]
    for (const access of contradictory) {
      expect(projectAccess(project(access), other, 8)).toMatchObject({ ready: false, canRead: false, canEdit: false })
    }
  })

  it('个人任务面板：作者、编辑者与查看者协作者显示，纯管理员代管不显示', () => {
    const at = (snapshot: ProjectAccess, actor: typeof user) => showsTaskPanel(8, projectAccess(project(snapshot), actor, 8))
    expect(showsTaskPanel(null, projectAccess(null, user, NaN))).toBe(true)
    expect(at(owner, user)).toBe(true)
    expect(at(editor, other)).toBe(true)
    expect(at(viewer, other)).toBe(true)
    expect(at(admin, other)).toBe(false)
    expect(at(denied, other)).toBe(false)
  })

  it('任务输出：协作者只订阅白名单类型，查看者只读已结束输出', () => {
    const base = { sessionCurrent: true, scoped: true, canRead: true, canUseAI: true, isCollaborator: true }
    expect(taskOutputMode({ ...base, live: true, kind: 'chapter.generate' })).toBe('stream')
    expect(taskOutputMode({ ...base, live: true, kind: 'wizard.world' })).toBe('stream')
    for (const kind of ['agent.turn', 'kb.index', 'book.import'] as const) {
      expect(taskOutputMode({ ...base, live: true, kind })).toBe('status')
      expect(taskOutputMode({ ...base, live: false, kind })).toBe('output')
      // 作者（非协作者）仍订阅全部类型
      expect(taskOutputMode({ ...base, isCollaborator: false, live: true, kind })).toBe('stream')
    }
    expect(taskOutputMode({ ...base, canUseAI: false, live: true, kind: 'chapter.generate' })).toBe('status')
    expect(taskOutputMode({ ...base, canUseAI: false, live: false, kind: 'chapter.generate' })).toBe('output')
    expect(taskOutputMode({ ...base, canRead: false, canUseAI: false, live: false, kind: 'chapter.generate' })).toBe('denied')
    expect(taskOutputMode({ ...base, sessionCurrent: false, live: false, kind: 'chapter.generate' })).toBe('denied')
    expect(taskOutputMode({ ...base, scoped: false, canRead: false, canUseAI: false, live: true, kind: 'kb.index' })).toBe('stream')
  })

  it('任务面板写操作：当前作品内无 AI 授权时隐藏，其他作品或个人任务交后端判定', () => {
    const collab = { ...project(viewer), owner: { id: 3, username: '作者' } }
    expect(taskControlsAllowed(8, project(owner), user)).toBe(true)
    expect(taskControlsAllowed(8, { ...project(editor), owner: { id: 3, username: '作者' } }, user)).toBe(true)
    expect(taskControlsAllowed(8, collab, user)).toBe(false)
    expect(taskControlsAllowed(8, project(admin), user)).toBe(false)
    // 不属于当前作品、个人任务、未加载作品：不在前端猜测
    expect(taskControlsAllowed(9, collab, user)).toBe(true)
    expect(taskControlsAllowed(null, collab, user)).toBe(true)
    expect(taskControlsAllowed(8, null, user)).toBe(true)
  })

  it('删除名称必须完整准确', () => {
    expect(canConfirmProjectDelete('星河', '星河')).toBe(true)
    expect(canConfirmProjectDelete('', '星河')).toBe(false)
    expect(canConfirmProjectDelete('星河 ', '星河')).toBe(false)
  })
})

describe('AI 模型来源（作者模型回退）', () => {
  const shared = (access: ProjectAccess) => ({ id: 8, owner: { id: 3, username: '作者' }, access })
  const of = (access: ProjectAccess, mine = access.role === 'owner') => projectAccess(mine ? project(access) : shared(access), user, 8)
  const ungranted: ProjectAccess = { ...editor, allow_owner_ai: false }

  it('作者无本人配置 → none；作者永远不回退', () => {
    expect(aiModelSource(of(owner), true, 0)).toBe('none')
    expect(aiModelSource(of({ ...owner, allow_owner_ai: true }), true, 0)).toBe('none')
    expect(aiModelSource(of(owner), true, 1)).toBe('own')
  })

  it('编辑者：无配置且已授权 → owner；未授权 → none；有配置 → own', () => {
    expect(aiModelSource(of(editor), true, 0)).toBe('owner')
    expect(aiModelSource(of(ungranted), true, 0)).toBe('none')
    expect(aiModelSource(of(editor), true, 2)).toBe('own')
    expect(aiModelSource(of(ungranted), true, 1)).toBe('own')
  })

  it('查看者、纯管理员、无授权 → none（即使有本人配置）', () => {
    for (const access of [viewer, { ...viewer, allow_owner_ai: true }, admin, denied]) {
      expect(aiModelSource(of(access), true, 0)).toBe('none')
      expect(aiModelSource(of(access), true, 1)).toBe('none')
    }
  })

  it('加载中不误判：本人配置未加载或作品快照未就绪', () => {
    expect(aiModelSource(of(editor), false, 0)).toBe('loading')
    expect(aiModelSource(of(owner), false, 0)).toBe('loading')
    expect(aiModelSource(projectAccess(null, user, 8), true, 0)).toBe('loading')
    expect(aiModelSource(projectAccess(shared(editor), user, 9), true, 0)).toBe('loading')
  })

  it('作品外上下文只看本人配置', () => {
    expect(aiModelSource(null, false, 0)).toBe('loading')
    expect(aiModelSource(null, true, 0)).toBe('none')
    expect(aiModelSource(null, true, 1)).toBe('own')
  })
})

describe('服务端判定不可直接重试的任务（E6-f 裁决 19）', () => {
  it('只看 retry_blocked_reason，不按标题识别、不依赖当前作品', () => {
    expect(taskRetryBlockedHint({ retry_blocked_reason: 'memory_bound' })).toBe(COLLABORATOR_MEMORY_RETRY_HINT)
    expect(taskRetryBlockedHint({ retry_blocked_reason: 'legacy_index' })).toContain('重新索引该文档')
    expect(taskRetryBlockedHint({ retry_blocked_reason: 'binding_invalid' })).toContain('无法直接重试')
    expect(taskRetryBlockedHint({ retry_blocked_reason: 'request_invalid' })).toContain('无法直接重试')
    // 服务端未标记（含旧后端缺省字段）：保持原重试行为，即使标题是「剧情记忆向量」。
    expect(taskRetryBlockedHint({ retry_blocked_reason: null })).toBeNull()
    expect(taskRetryBlockedHint({} as Pick<Task, 'retry_blocked_reason'>)).toBeNull()
  })
})
