import { EyeOutlined } from '@ant-design/icons'
import { Alert, Button, Input, Modal, Select, Space, Spin, Typography } from 'antd'
import { useId, useState } from 'react'
import { errorMessage } from '../../api/client'
import { skillApi } from '../../api/skills'
import type { SkillBrief, SkillInputValues, SkillPreviewOut } from '../../api/types'
import { inputProblem, resolveInputValues, SKILL_INPUT_VALUE_LIMITS } from '../../utils/skillInputs'

interface Props {
  skill: SkillBrief | null
  value: SkillInputValues
  onChange: (value: SkillInputValues) => void
  disabled?: boolean
}

/** 选中 Skill 的输入项表单（受控）：按类型渲染单行 / 多行 / 下拉，带必填标记与说明；「预览」显示填入后的正文。没有输入项时不渲染。 */
export default function SkillInputsForm({ skill, value, onChange, disabled }: Props) {
  const baseId = useId()
  const [preview, setPreview] = useState<{ loading: boolean; result: SkillPreviewOut | null; error: string | null } | null>(null)
  if (!skill || skill.inputs.length === 0) return null
  const values = resolveInputValues(skill.inputs, value)
  const set = (name: string, next: string) => onChange({ ...values, [name]: next })
  const labelOf = (name: string) => skill.inputs.find((d) => d.name === name)?.label ?? name

  const openPreview = async () => {
    setPreview({ loading: true, result: null, error: null })
    try {
      const result = await skillApi.preview(skill.id, values)
      setPreview({ loading: false, result, error: null })
    } catch (err) {
      setPreview({ loading: false, result: null, error: errorMessage(err) })
    }
  }

  return (
    <div role="group" aria-label={`Skill「${skill.name}」的输入项`} style={{ width: '100%', marginTop: 8 }}>
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        {skill.inputs.map((def, index) => {
          const id = `${baseId}-${index}`
          const current = values[def.name] ?? ''
          const problem = inputProblem(def, current)
          const status = problem ? 'error' : undefined
          const limit = SKILL_INPUT_VALUE_LIMITS[def.kind]
          return (
            <div key={def.name}>
              <label htmlFor={id} style={{ display: 'block', marginBottom: 4 }}>
                {def.required && <Typography.Text type="danger" aria-hidden style={{ marginRight: 4 }}>*</Typography.Text>}
                {def.label}
                {def.required && <Typography.Text type="secondary" style={{ fontSize: 12 }}>（必填）</Typography.Text>}
              </label>
              {def.kind === 'select' ? (
                <Select
                  id={id}
                  aria-required={def.required}
                  value={current || undefined}
                  allowClear={!def.required}
                  onChange={(v?: string) => set(def.name, v ?? '')}
                  options={def.options.map((o) => ({ value: o, label: o }))}
                  disabled={disabled}
                  status={status}
                  placeholder="请选择"
                  style={{ width: '100%' }}
                />
              ) : def.kind === 'textarea' ? (
                <Input.TextArea
                  id={id}
                  aria-required={def.required}
                  value={current}
                  maxLength={limit}
                  autoSize={{ minRows: 2, maxRows: 6 }}
                  onChange={(e) => set(def.name, e.target.value)}
                  disabled={disabled}
                  status={status}
                />
              ) : (
                <Input id={id} aria-required={def.required} value={current} maxLength={limit} onChange={(e) => set(def.name, e.target.value)} disabled={disabled} status={status} />
              )}
              {def.help && <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block' }}>{def.help}</Typography.Text>}
              {problem && <Typography.Text type="danger" style={{ fontSize: 12, display: 'block' }}>{problem === '必填' ? '必填项，请填写' : problem}</Typography.Text>}
            </div>
          )
        })}
        <Button size="small" icon={<EyeOutlined />} onClick={() => void openPreview()}>
          预览填入后的 Skill 正文
        </Button>
      </Space>
      <Modal open={preview !== null} title={`预览：${skill.name}`} footer={null} onCancel={() => setPreview(null)} destroyOnHidden>
        <Spin spinning={!!preview?.loading}>
          <Space direction="vertical" style={{ width: '100%' }}>
            {preview?.error && <Alert type="error" showIcon message={preview.error} />}
            {preview?.result && preview.result.missing_required.length > 0 && (
              <Alert type="warning" showIcon message={`必填项未填写：${preview.result.missing_required.map(labelOf).join('、')}`} />
            )}
            {preview?.result?.errors.map((e) => <Alert key={e} type="error" showIcon message={e} />)}
            {preview?.result && (
              <>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>以下为填入当前值、展开组件后的正文，即实际注入的补充工作流。</Typography.Text>
                <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 420, overflow: 'auto', margin: 0 }}>
                  {preview.result.body || '（正文为空）'}
                </pre>
              </>
            )}
          </Space>
        </Spin>
      </Modal>
    </div>
  )
}
