import { useCallback, useMemo, useState } from 'react'
import type { SkillBrief, SkillInputValues } from '../api/types'
import { useSkills } from '../stores/skills'
import { defaultInputValues, skillChoiceState, type SkillChoiceState } from '../utils/skillInputs'

export interface SkillChoice extends SkillChoiceState {
  skillId: number | null
  /** 已填写的值（未出现的输入项按默认值计；以 values 为准） */
  inputs: SkillInputValues
  /** 切换 Skill：清空上一个 Skill 的填写内容，并以新 Skill 的默认值预填 */
  setSkillId: (id: number | null) => void
  setInputs: (inputs: SkillInputValues) => void
}

/** 切换 Skill 时的初始值：新 Skill 的默认值（列表未加载时为空，取值时仍按默认值补齐） */
export function initialInputs(skills: SkillBrief[], id: number | null): SkillInputValues {
  const skill = id === null ? undefined : skills.find((s) => s.id === id)
  return skill ? defaultInputValues(skill.inputs) : {}
}

/**
 * 手动选用 Skill 的入口共用：选中的 Skill + 输入项的值。
 * `payload` 展开到请求里：只在选了 Skill 时带 skill_id，只有 Skill 声明了输入项时才带 skill_inputs；必填为空时 `valid = false`。
 */
export function useSkillChoice(): SkillChoice {
  const { skills } = useSkills()
  const [state, setState] = useState<{ skillId: number | null; inputs: SkillInputValues }>({ skillId: null, inputs: {} })
  const setSkillId = useCallback(
    (id: number | null) => setState((prev) => (prev.skillId === id ? prev : { skillId: id, inputs: initialInputs(skills, id) })),
    [skills],
  )
  const setInputs = useCallback((inputs: SkillInputValues) => setState((prev) => ({ ...prev, inputs })), [])
  const derived = useMemo(() => skillChoiceState(skills, state.skillId, state.inputs), [skills, state.skillId, state.inputs])
  return { ...derived, skillId: state.skillId, inputs: state.inputs, setSkillId, setInputs }
}
