import { afterEach, describe, expect, it, vi } from 'vitest'
import { skillApi } from '../api/skills'
import { styleApi } from '../api/styles'
import { aiConfigApi } from '../api/aiConfigs'
import { useSkillStore } from './skills'
import { useStyleStore } from './styles'
import { useAIConfigStore } from './aiConfigs'
import { deferred } from '../components/systemSettings/systemSettingsTestFixtures'
import type { SkillBrief } from '../api/types'
const oldSkill: SkillBrief = { id: 1, scope: 'mine', slug: 'old', name: '旧账号私有内容', description: '', category: '', triggers: [], builtin_key: null, source_skill_id: null, editable: true, body_chars: 10, reference_titles: [], created_at: '', updated_at: '' }

afterEach(() => { useSkillStore.getState().reset(); useStyleStore.getState().reset(); useAIConfigStore.getState().reset(); vi.restoreAllMocks() })

describe('账号缓存代次隔离', () => {
  it('旧账号私人Skill列表迟到不能覆盖新账号，新加载不被旧finally取消loading', async () => {
    const old = deferred<Awaited<ReturnType<typeof skillApi.list>>>()
    const fresh = deferred<Awaited<ReturnType<typeof skillApi.list>>>()
    const list = vi.spyOn(skillApi, 'list').mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise)
    const pending = useSkillStore.getState().load()
    await vi.waitFor(() => expect(list).toHaveBeenCalledOnce())
    useSkillStore.getState().reset()
    const current = useSkillStore.getState().load()
    await vi.waitFor(() => expect(list).toHaveBeenCalledTimes(2))
    old.resolve([oldSkill]); await pending
    expect(useSkillStore.getState()).toMatchObject({ skills: [], loading: true })
    fresh.resolve([]); await current
    expect(useSkillStore.getState()).toMatchObject({ skills: [], loading: false, loaded: true })
  })
  it('风格与模型列表同样作废旧请求，reset释放loading允许新账号加载', async () => {
    const oldStyle = deferred<Awaited<ReturnType<typeof styleApi.list>>>()
    const oldConfig = deferred<Awaited<ReturnType<typeof aiConfigApi.list>>>()
    vi.spyOn(styleApi, 'list').mockReturnValueOnce(oldStyle.promise).mockResolvedValueOnce([])
    vi.spyOn(aiConfigApi, 'list').mockReturnValueOnce(oldConfig.promise).mockResolvedValueOnce([])
    const styles = useStyleStore.getState().load(), configs = useAIConfigStore.getState().load()
    useStyleStore.getState().reset(); useAIConfigStore.getState().reset()
    await useStyleStore.getState().load(); await useAIConfigStore.getState().load()
    oldStyle.reject(new Error('旧风格错误')); oldConfig.resolve([])
    await styles; await configs
    expect(useStyleStore.getState()).toMatchObject({ styles: [], loaded: true, loading: false, error: null })
    expect(useAIConfigStore.getState()).toMatchObject({ configs: [], loaded: true, loading: false })
  })
})
