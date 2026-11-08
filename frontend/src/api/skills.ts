// Skill 库（/api/v1/skills）：我的 / 全局（管理员发布）、参考资料、复制、导入导出、恢复内置内容

import { api } from './client'
import { downloadAttachment } from './projects'
import type { Skill, SkillBrief, SkillCreate, SkillInputValues, SkillPreviewOut, SkillReferenceInput, SkillScope, SkillUpdate } from './types'

export const skillApi = {
  list: (scope: SkillScope | 'all' = 'all') => api.get<SkillBrief[]>('/skills', { scope }),
  get: (id: number) => api.get<Skill>(`/skills/${id}`),
  /** scope = global 需要管理员 */
  create: (body: SkillCreate) => api.post<Skill>('/skills', body),
  /** 全局 Skill 非管理员 → 403 */
  update: (id: number, body: SkillUpdate) => api.patch<Skill>(`/skills/${id}`, body),
  remove: (id: number) => api.del(`/skills/${id}`),
  /** 整体替换参考资料列表（顺序即 sort） */
  replaceReferences: (id: number, references: SkillReferenceInput[]) =>
    api.put<Skill>(`/skills/${id}/references`, { references }),
  /** 填入输入值、展开组件后的正文（即实际注入的补充工作流）；不落库，可见即可预览 */
  preview: (id: number, inputs: SkillInputValues, signal?: AbortSignal) =>
    api.post<SkillPreviewOut>(`/skills/${id}/preview`, { inputs }, signal),
  /** 复制为我的副本（slug 冲突时追加 -2、-3） */
  copy: (id: number) => api.post<Skill>(`/skills/${id}/copy`),
  /** 仅管理员、仅内置 Skill */
  restoreBuiltin: (id: number) => api.post<Skill>(`/skills/${id}/restore-builtin`),
  /** SKILL.md 或 zip（SKILL.md + references/*.md），≤ 2MB */
  import: (file: File, scope: SkillScope = 'mine') => {
    const form = new FormData()
    form.append('file', file)
    form.append('scope', scope)
    return api.upload<Skill>('/skills/import', form)
  },
  /** 有参考资料时为 zip，否则为单个 SKILL.md */
  export: async (id: number): Promise<string> =>
    (await downloadAttachment(`/skills/${id}/export`, {}, 'SKILL.md')).filename,
}

/** 与后端 skill_service / schemas/skill.py 一致 */
export const SKILL_LIMITS = {
  name: 100,
  description: 500,
  category: 30,
  trigger: 30,
  triggers: 10,
  body: 50000,
  references: 20,
  referenceTitle: 100,
  referenceContent: 100000,
  uploadBytes: 2 * 1024 * 1024,
} as const

/** 与后端 app/ai/skill_inputs.py 一致 */
export const SKILL_INPUT_LIMITS = {
  inputs: 20,
  label: 30,
  help: 200,
  option: 50,
  minOptions: 2,
  maxOptions: 50,
  /** 各类型取值上限（字数） */
  value: { text: 200, textarea: 5000, select: 50 },
} as const

export const SKILL_SLUG_RE = /^[a-z0-9][a-z0-9-]{0,47}$/
