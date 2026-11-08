import type { CSSProperties } from 'react'
import type { SkillChoice } from '../../hooks/useSkillChoice'
import SkillInputsForm from './SkillInputsForm'
import SkillSelect from './SkillSelect'

interface Props {
  choice: Pick<SkillChoice, 'skillId' | 'skill' | 'values' | 'setSkillId' | 'setInputs'>
  disabled?: boolean
  placeholder?: string
  style?: CSSProperties
  'aria-label'?: string
}

/** Skill 选择 + 选中 Skill 的输入项表单（`useSkillChoice()` 的状态）；手动选用 Skill 的入口共用。 */
export default function SkillChoiceField({ choice, disabled, placeholder, style, ...rest }: Props) {
  return (
    <div style={{ width: '100%', ...style }}>
      <SkillSelect aria-label={rest['aria-label']} value={choice.skillId} onChange={choice.setSkillId} disabled={disabled} placeholder={placeholder} />
      <SkillInputsForm skill={choice.skill} value={choice.values} onChange={choice.setInputs} disabled={disabled} />
    </div>
  )
}
