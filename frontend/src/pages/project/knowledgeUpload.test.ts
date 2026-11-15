import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { kbApi } from '../../api/kb'
import { projectApi } from '../../api/projects'
import type { KbDocument, KbUploadResult, Project, ProjectAccess, Task, User } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { captureProjectScope, useProjectStore } from '../../stores/project'
import { knowledgeUploadError, knowledgeUploadMode, knowledgeUploadNotice, uploadKnowledgeForScope } from './knowledgeUpload'

const editor: User = { id: 1, username: '编辑者', created_at: '', is_admin: false, is_super_admin: false,
  effective_permissions: [], roles: [], authorization_revision: 1 }
const editorAccess: ProjectAccess = { role: 'editor', is_collaborator: true, authorization_version: 'grant-1',
  allow_owner_ai: false, can_read: true, can_edit_content: true, can_use_ai: true, can_export: true,
  can_manage_collaborators: false, can_recycle: false, can_backup: false, can_write_reading_progress: false }
function project(access = editorAccess): Project {
  return { id: 2, owner: { id: 9, username: '作者' }, access, title: '作品', genre: '', premise: '', status: 'planning',
    created_at: '', updated_at: '', chapter_count: 0, total_words: 0, cover_updated_at: null, auto_analyze: true,
    wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null, default_style: null,
    synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null, last_edited_chapter_id: null, reading: null }
}
const doc: KbDocument = { id: 5, project_id: 2, filename: '资料.md', mime: 'text/markdown', size: 4, sha256: 'sha',
  status: 'ready_keyword_only', error: '', warnings: [], char_count: 4, chunk_count: 1, inject: true, created_at: '', updated_at: '' }
const task = { id: 8, task_key: '36ac2770-75ac-4c37-afd4-357f954cae9b', project_id: 2, status: 'queued' } as Task
const file = new File(['资料'], '资料.md')
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done }); return { promise, resolve } }

beforeEach(() => { useProjectStore.getState().reset(); useAuthStore.setState({ status: 'authenticated', user: editor }) })
afterEach(() => { useProjectStore.getState().reset(); vi.restoreAllMocks() })

describe('知识库上传与真实索引状态', () => {
  it('作者保留原上传流程；只有真实编辑者可上传（索引方式由服务端决定）', () => {
    expect(knowledgeUploadMode(project(), editor, 2)).toBe('collaborator')
    const owner: User = { ...editor, id: 9, username: '作者' }
    const ownerProject = project({ ...editorAccess, role: 'owner', is_collaborator: false, authorization_version: null })
    expect(knowledgeUploadMode(ownerProject, owner, 2)).toBe('owner')
    // E6-c：作者授权与否都可上传，是否使用作者向量由服务端按授权与作者配置决定
    expect(knowledgeUploadMode(project({ ...editorAccess, allow_owner_ai: true }), editor, 2)).toBe('collaborator')
    expect(knowledgeUploadMode(project({ ...editorAccess, can_edit_content: false }), editor, 2)).toBe('none')
    expect(knowledgeUploadMode(project({ ...editorAccess, can_use_ai: false }), editor, 2)).toBe('none')
    const admin = { ...editor, is_admin: true }
    expect(knowledgeUploadMode(project({ ...editorAccess, role: 'viewer' }), admin, 2)).toBe('none')
    expect(knowledgeUploadMode(project({ ...editorAccess, role: 'admin', is_collaborator: false, authorization_version: null }), admin, 2)).toBe('none')
    expect(knowledgeUploadMode(project(), editor, 3)).toBe('none')
  })

  it('空任务只显示关键词索引和本次无向量费用，不登记后台任务', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project())
    await useProjectStore.getState().load(2)
    vi.spyOn(kbApi, 'upload').mockResolvedValue({ document: doc, task: null })
    const track = vi.fn()
    const result = await uploadKnowledgeForScope(2, captureProjectScope(2), file, track)
    expect(track).not.toHaveBeenCalled()
    expect(result).toEqual({ document: doc, task: null })
    expect(knowledgeUploadNotice(result as KbUploadResult)).toEqual({ type: 'success',
      text: '《资料.md》已上传，仅建立关键词索引；未调用作者向量模型，不产生相应费用。' })
  })

  it('作者收到真实任务时登记任务；未知文档状态不伪称关键词索引已完成', async () => {
    const owner = { ...editor, id: 9, username: '作者' }
    useAuthStore.setState({ status: 'authenticated', user: owner })
    vi.spyOn(projectApi, 'get').mockResolvedValue(project({ ...editorAccess, role: 'owner',
      is_collaborator: false, authorization_version: null }))
    await useProjectStore.getState().load(2)
    vi.spyOn(kbApi, 'upload').mockResolvedValue({ document: { ...doc, status: 'pending' }, task })
    const track = vi.fn()
    const result = await uploadKnowledgeForScope(2, captureProjectScope(2), file, track)
    expect(track).toHaveBeenCalledExactlyOnceWith(task)
    expect(knowledgeUploadNotice(result as KbUploadResult)).toEqual({ type: 'success', text: '《资料.md》已上传，索引任务已创建。' })
    expect(knowledgeUploadNotice(result as KbUploadResult, true)).toEqual({ type: 'success',
      text: '《资料.md》已上传，索引任务已创建；使用作者共享的向量模型，费用由作者承担。' })
    expect(knowledgeUploadNotice({ document: { ...doc, status: 'pending' }, task: null })).toEqual({
      type: 'warning', text: '《资料.md》已上传，索引状态待确认，请刷新查看。',
    })
  })

  it('同账号退出重登后，旧上传响应不再登记任务或通知新会话', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project())
    await useProjectStore.getState().load(2)
    const pending = deferred<KbUploadResult>()
    const upload = vi.spyOn(kbApi, 'upload').mockReturnValue(pending.promise)
    const track = vi.fn()
    const old = captureProjectScope(2)
    const result = uploadKnowledgeForScope(2, old, file, track)
    useAuthStore.getState().clear()
    useAuthStore.setState({ status: 'authenticated', user: editor })
    await useProjectStore.getState().load(2)
    pending.resolve({ document: doc, task })
    expect(await result).toBeNull()
    expect(track).not.toHaveBeenCalled()
    expect(await uploadKnowledgeForScope(2, old, file, track)).toBeNull()
    expect(upload).toHaveBeenCalledOnce()
  })

  it('同能力的授权轮次变化后，迟到的旧任务不能登记到新授权', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project())
      .mockResolvedValueOnce(project({ ...editorAccess, authorization_version: 'grant-2' }))
    await useProjectStore.getState().load(2)
    const pending = deferred<KbUploadResult>()
    vi.spyOn(kbApi, 'upload').mockReturnValue(pending.promise)
    const track = vi.fn()
    const result = uploadKnowledgeForScope(2, captureProjectScope(2), file, track)
    await useProjectStore.getState().refresh()
    pending.resolve({ document: doc, task })
    expect(await result).toBeNull()
    expect(track).not.toHaveBeenCalled()
  })

  it('跨作品的异常响应不登记任务，需要人工核对', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project())
    await useProjectStore.getState().load(2)
    vi.spyOn(kbApi, 'upload').mockResolvedValue({ document: doc, task: { ...task, project_id: 3 } })
    const track = vi.fn()
    await expect(uploadKnowledgeForScope(2, captureProjectScope(2), file, track)).rejects.toThrow('请刷新核对')
    expect(track).not.toHaveBeenCalled()
  })

  it('请求结果未知时只要求刷新核对，不自动重发；明确的重复文件仍显示原提示', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project())
    await useProjectStore.getState().load(2)
    const failure = new ApiError(0, '网络错误')
    const upload = vi.spyOn(kbApi, 'upload').mockRejectedValue(failure)
    await expect(uploadKnowledgeForScope(2, captureProjectScope(2), file, vi.fn())).rejects.toBe(failure)
    expect(upload).toHaveBeenCalledOnce()
    for (const status of [0, 403, 404, 500]) {
      expect(knowledgeUploadError(new ApiError(status, '失败'))).toEqual({
        type: 'warning', text: '上传结果未确认，请刷新文档列表核对后再操作。',
      })
    }
    expect(knowledgeUploadError(new ApiError(409, '文件已存在', { document_id: 5 })))
      .toEqual({ type: 'warning', text: '文件已存在' })
  })
})
