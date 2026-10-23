import { Form, Input, InputNumber, Switch } from 'antd'
import { writeAutoContinue } from '../../utils/preferences'

export interface GenerateOptions {
  targetWords: number
  instruction: string
  autoContinue: boolean
}

interface GenerateProps {
  value: GenerateOptions
  onChange: (value: GenerateOptions) => void
  disabled: boolean
}

/** 整章生成参数：目标字数、自动续写开关（记入 localStorage）、补充指令。 */
export function GenerateForm({ value, onChange, disabled }: GenerateProps) {
  return (
    <Form layout="vertical" disabled={disabled}>
      <Form.Item label="目标字数">
        <InputNumber
          min={300}
          max={20000}
          step={500}
          value={value.targetWords}
          onChange={(v) => onChange({ ...value, targetWords: v ?? 3000 })}
          style={{ width: '100%' }}
        />
      </Form.Item>
      <Form.Item label="字数不足时自动续写" tooltip="首轮正文不足目标字数的 85% 时，自动接着前文续写，最多 2 轮">
        <Switch
          checked={value.autoContinue}
          onChange={(v) => {
            writeAutoContinue(v)
            onChange({ ...value, autoContinue: v })
          }}
        />
      </Form.Item>
      <Form.Item label="补充指令（可选）">
        <Input.TextArea
          value={value.instruction}
          onChange={(e) => onChange({ ...value, instruction: e.target.value })}
          maxLength={2000}
          autoSize={{ minRows: 3, maxRows: 8 }}
          placeholder="例如：本章重点描写主角与师父的冲突，结尾留悬念"
        />
      </Form.Item>
    </Form>
  )
}

interface PolishProps {
  value: string
  onChange: (value: string) => void
  disabled: boolean
}

/** 整章润色要求。 */
export function PolishForm({ value, onChange, disabled }: PolishProps) {
  return (
    <Form layout="vertical" disabled={disabled}>
      <Form.Item label="润色要求（可选）">
        <Input.TextArea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          maxLength={2000}
          autoSize={{ minRows: 3, maxRows: 8 }}
          placeholder="例如：增强画面感，精简对话，统一人称"
        />
      </Form.Item>
    </Form>
  )
}
