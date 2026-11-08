import { ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { Button, Card, Checkbox, Form, Input, Select, Space, Typography } from 'antd'
import type { FormInstance } from 'antd'
import { SKILL_INPUT_LIMITS } from '../../api/skills'
import type { SkillInputDef, SkillInputKind } from '../../api/types'
import { SKILL_INPUT_KIND_LABELS, SKILL_INPUT_NAME_RE, SKILL_INPUT_VALUE_LIMITS } from '../../utils/skillInputs'

interface Props {
  /** 所在表单（字段 inputs） */
  form: FormInstance
  readOnly: boolean
  /** 在正文光标处插入 {{ 标识 }} */
  onInsert?: (name: string) => void
}

const EMPTY: SkillInputDef = { name: '', label: '', help: '', kind: 'text', required: false, default: '', options: [] }
const KIND_OPTIONS = (Object.keys(SKILL_INPUT_KIND_LABELS) as SkillInputKind[]).map((k) => ({ value: k, label: SKILL_INPUT_KIND_LABELS[k] }))
const charCount = (text: string) => Array.from(text).length

/** Skill 输入项编辑器（`Form.List name="inputs"`）：增删、上下移动、类型切换、必填、默认值、下拉选项。正文用 {{ 标识 }} 引用。 */
export default function SkillInputsEditor({ form, readOnly, onInsert }: Props) {
  return (
    <fieldset disabled={readOnly} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
      <Form.List name="inputs">
        {(fields, { add, remove, move }) => (
          <Space direction="vertical" style={{ width: '100%' }}>
            {fields.map((field, index) => (
              <Card
                key={field.key}
                size="small"
                title={`输入项 ${index + 1}`}
                extra={
                  !readOnly && (
                    <Space size={0}>
                      {onInsert && (
                        <Button type="link" size="small" onClick={() => {
                          const name = String(form.getFieldValue(['inputs', field.name, 'name']) ?? '').trim()
                          if (name) onInsert(name)
                        }}>
                          插入到正文
                        </Button>
                      )}
                      <Button type="text" size="small" aria-label="上移输入项" icon={<ArrowUpOutlined />} disabled={index === 0} onClick={() => move(index, index - 1)} />
                      <Button type="text" size="small" aria-label="下移输入项" icon={<ArrowDownOutlined />} disabled={index === fields.length - 1} onClick={() => move(index, index + 1)} />
                      <Button type="text" size="small" danger aria-label="删除输入项" icon={<DeleteOutlined />} onClick={() => remove(field.name)} />
                    </Space>
                  )
                }
              >
                <Space wrap align="start" size={[12, 0]} style={{ width: '100%' }}>
                  <Form.Item
                    name={[field.name, 'name']}
                    label="标识"
                    tooltip="正文里写 {{ 标识 }} 引用该输入值"
                    rules={[
                      { required: true, whitespace: true, message: '请输入标识' },
                      { pattern: SKILL_INPUT_NAME_RE, message: '字母、数字、下划线或汉字，不以数字开头，≤32 字' },
                      {
                        validator: (_, value: string = '') => {
                          const names = ((form.getFieldValue('inputs') ?? []) as SkillInputDef[]).map((d) => (d?.name ?? '').trim())
                          return value.trim() && names.filter((n) => n === value.trim()).length > 1
                            ? Promise.reject(new Error('标识重复'))
                            : Promise.resolve()
                        },
                      },
                    ]}
                  >
                    <Input disabled={readOnly} maxLength={32} placeholder="例如 tone" style={{ width: 160 }} />
                  </Form.Item>
                  <Form.Item
                    name={[field.name, 'label']}
                    label="显示名"
                    rules={[{ required: true, whitespace: true, message: '请输入显示名' }]}
                  >
                    <Input disabled={readOnly} maxLength={SKILL_INPUT_LIMITS.label} placeholder="例如 基调" style={{ width: 160 }} />
                  </Form.Item>
                  <Form.Item name={[field.name, 'kind']} label="类型">
                    <Select disabled={readOnly} options={KIND_OPTIONS} style={{ width: 120 }} aria-label="输入项类型" />
                  </Form.Item>
                  <Form.Item name={[field.name, 'required']} label="必填" valuePropName="checked">
                    <Checkbox disabled={readOnly}>必填</Checkbox>
                  </Form.Item>
                </Space>
                <Form.Item noStyle dependencies={[['inputs', field.name, 'kind'], ['inputs', field.name, 'options']]}>
                  {() => {
                    const kind = (form.getFieldValue(['inputs', field.name, 'kind']) ?? 'text') as SkillInputKind
                    const options = ((form.getFieldValue(['inputs', field.name, 'options']) ?? []) as string[]).map((o) => o.trim()).filter(Boolean)
                    const limit = SKILL_INPUT_VALUE_LIMITS[kind]
                    return (
                      <>
                        {kind === 'select' && (
                          <Form.Item
                            name={[field.name, 'options']}
                            label="选项"
                            tooltip={`${SKILL_INPUT_LIMITS.minOptions}–${SKILL_INPUT_LIMITS.maxOptions} 项，每项不超过 ${SKILL_INPUT_LIMITS.option} 字；输入后回车`}
                            rules={[{
                              validator: (_, value: string[] = []) => {
                                const list = value.map((o) => o.trim()).filter(Boolean)
                                if (list.length < SKILL_INPUT_LIMITS.minOptions || list.length > SKILL_INPUT_LIMITS.maxOptions)
                                  return Promise.reject(new Error(`需要 ${SKILL_INPUT_LIMITS.minOptions}–${SKILL_INPUT_LIMITS.maxOptions} 个选项`))
                                if (list.some((o) => charCount(o) > SKILL_INPUT_LIMITS.option))
                                  return Promise.reject(new Error(`每个选项不超过 ${SKILL_INPUT_LIMITS.option} 字`))
                                return Promise.resolve()
                              },
                            }]}
                          >
                            <Select disabled={readOnly} mode="tags" open={false} tokenSeparators={['\n']} placeholder="输入选项后回车" aria-label="下拉选项" />
                          </Form.Item>
                        )}
                        <Form.Item
                          name={[field.name, 'default']}
                          label="默认值"
                          dependencies={[['inputs', field.name, 'options'], ['inputs', field.name, 'kind']]}
                          rules={[{
                            validator: (_, value: string = '') => {
                              if (kind === 'select' && value && !options.includes(value)) return Promise.reject(new Error('默认值须是选项之一'))
                              if (charCount(value) > limit) return Promise.reject(new Error(`不超过 ${limit} 字`))
                              return Promise.resolve()
                            },
                          }]}
                        >
                          {kind === 'select' ? (
                            <Select disabled={readOnly} allowClear placeholder="不预选" options={options.map((o) => ({ value: o, label: o }))} aria-label="默认值" />
                          ) : kind === 'textarea' ? (
                            <Input.TextArea disabled={readOnly} maxLength={limit} autoSize={{ minRows: 2, maxRows: 6 }} />
                          ) : (
                            <Input disabled={readOnly} maxLength={limit} />
                          )}
                        </Form.Item>
                      </>
                    )
                  }}
                </Form.Item>
                <Form.Item name={[field.name, 'help']} label="说明（可选）" style={{ marginBottom: 0 }}>
                  <Input disabled={readOnly} maxLength={SKILL_INPUT_LIMITS.help} placeholder="填写时显示在输入框下方" />
                </Form.Item>
              </Card>
            ))}
            {!readOnly && (
              <Button icon={<PlusOutlined />} disabled={fields.length >= SKILL_INPUT_LIMITS.inputs} onClick={() => add({ ...EMPTY })}>
                添加输入项{fields.length >= SKILL_INPUT_LIMITS.inputs ? `（最多 ${SKILL_INPUT_LIMITS.inputs} 个）` : ''}
              </Button>
            )}
            {readOnly && fields.length === 0 && <Typography.Text type="secondary">没有输入项</Typography.Text>}
          </Space>
        )}
      </Form.List>
    </fieldset>
  )
}
