import { afterEach, describe, expect, it, vi } from 'vitest'
import { projectApi } from '../api/projects'
import type { Project } from '../api/types'
import { useProjectStore } from './project'

function project(id: number): Project {
  return { id, owner: { id, username: `user${id}` }, title: `书${id}`, genre: '', premise: '', status: 'planning', created_at: '', updated_at: '', chapter_count: 0, total_words: 0, cover_updated_at: null, auto_analyze: true, wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null, default_style: null, synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null, last_edited_chapter_id: null, reading: null }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

afterEach(() => { useProjectStore.getState().reset(); vi.restoreAllMocks() })

describe('当前项目归属隔离', () => {
  it('先打开的慢请求不能覆盖后来打开项目的内容和归属', async () => {
    const slow = deferred<Project>()
    vi.spyOn(projectApi, 'get').mockImplementation((id) => id === 1 ? slow.promise : Promise.resolve(project(2)))
    const first = useProjectStore.getState().load(1)
    await useProjectStore.getState().load(2)
    slow.resolve(project(1))
    await first
    expect(useProjectStore.getState().project).toEqual(project(2))
    expect(useProjectStore.getState().loading).toBe(false)
  })

  it('退出后不采用旧会话的加载响应', async () => {
    const slow = deferred<Project>()
    vi.spyOn(projectApi, 'get').mockReturnValue(slow.promise)
    const loading = useProjectStore.getState().load(1)
    useProjectStore.getState().reset()
    slow.resolve(project(1))
    await loading
    expect(useProjectStore.getState().project).toBeNull()
  })

  it('退出后不采用旧页面迟到的保存响应', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project(1))
    await useProjectStore.getState().load(1)
    useProjectStore.getState().reset()
    useProjectStore.getState().setProject(project(1))
    expect(useProjectStore.getState().project).toBeNull()
  })

  it('新会话重新加载同一项目时不采用旧页面的保存响应', async () => {
    const slow = deferred<Project>()
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project(1)).mockReturnValueOnce(slow.promise)
    await useProjectStore.getState().load(1)
    useProjectStore.getState().reset()
    const loading = useProjectStore.getState().load(1)
    useProjectStore.getState().setProject(project(1))
    expect(useProjectStore.getState().project).toBeNull()
    expect(useProjectStore.getState().loading).toBe(true)
    const fresh = { ...project(1), title: '新会话项目' }
    slow.resolve(fresh)
    await loading
    expect(useProjectStore.getState().project).toEqual(fresh)
  })

  it('只接受当前目标项目的保存响应', async () => {
    vi.spyOn(projectApi, 'get').mockImplementation((id) => Promise.resolve(project(id)))
    await useProjectStore.getState().load(2)
    useProjectStore.getState().setProject(project(1))
    expect(useProjectStore.getState().project?.id).toBe(2)
    useProjectStore.getState().setProject({ ...project(2), title: '已保存' })
    expect(useProjectStore.getState().project?.title).toBe('已保存')
  })
})
