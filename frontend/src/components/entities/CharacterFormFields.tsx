import { Col, Form, Input, InputNumber, Row, Select, Switch } from 'antd'
import type { FormInstance } from 'antd'
import { cleanAliases, LIFE_STATUS, roleTypeOptions } from '../../api/characters'
import type { LifeStatus } from '../../api/types'
import type { CharacterFormValues } from './characterForm'

const LIFE_OPTIONS = (Object.keys(LIFE_STATUS) as LifeStatus[]).map((k) => ({ value: k, label: LIFE_STATUS[k].label }))

interface BasicProps {
  form: FormInstance<CharacterFormValues>
  /** 新建时：定位选「主角」自动开启始终注入上下文 */
  creating?: boolean
  showSort?: boolean
}

/** 基础字段：名字、别名、定位（下拉 + 兼容旧值）、年龄、性别、存活状态、始终注入上下文。 */
export function CharacterBasicFields({ form, creating, showSort = true }: BasicProps) {
  const roleType = Form.useWatch('role_type', form)
  return (
    <>
      <Row gutter={16}>
        <Col span={12}>
          <Form.Item
            name="name"
            label="姓名"
            rules={[
              { required: true, whitespace: true, message: '请输入姓名' },
              { max: 100, message: '不超过 100 字' },
            ]}
          >
            <Input />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item name="role_type" label="定位" rules={[{ max: 50, message: '不超过 50 字' }]}>
            <Select
              allowClear
              options={roleTypeOptions(roleType)}
              placeholder="选择定位"
              onChange={(v: string | undefined) => {
                if (creating && v === '主角') form.setFieldValue('always_in_context', true)
              }}
            />
          </Form.Item>
        </Col>
      </Row>
      <Form.Item
        name="aliases"
        label="别名（回车添加，最多 10 个，每个 ≤50 字）"
        rules={[
          {
            validator: (_, value: string[] | undefined) => {
              const { error } = cleanAliases(value ?? [])
              return error ? Promise.reject(new Error(error)) : Promise.resolve()
            },
          },
        ]}
      >
        <Select mode="tags" tokenSeparators={[',', '，', '、']} open={false} placeholder="例如：小远、林师兄" />
      </Form.Item>
      <Row gutter={16}>
        <Col span={8}>
          <Form.Item name="age" label="年龄" rules={[{ max: 50, message: '不超过 50 字' }]}>
            <Input placeholder="例如：十七岁" />
          </Form.Item>
        </Col>
        <Col span={8}>
          <Form.Item name="gender" label="性别" rules={[{ max: 20, message: '不超过 20 字' }]}>
            <Input />
          </Form.Item>
        </Col>
        <Col span={8}>
          <Form.Item name="life_status" label="存活状态">
            <Select options={LIFE_OPTIONS} />
          </Form.Item>
        </Col>
      </Row>
      <Row gutter={16}>
        <Col span={12}>
          <Form.Item
            name="always_in_context"
            label="始终注入章节上下文"
            valuePropName="checked"
            tooltip="开启后每章生成都会带上该角色的完整设定（主角默认开启）"
          >
            <Switch />
          </Form.Item>
        </Col>
        {showSort && (
          <Col span={12}>
            <Form.Item name="sort" label="排序（越小越靠前，可留空）">
              <InputNumber style={{ width: 160 }} />
            </Form.Item>
          </Col>
        )}
      </Row>
    </>
  )
}

/** 描写字段：外貌、性格、背景、简介。 */
export function CharacterDescFields() {
  return (
    <>
      <Form.Item name="appearance" label="外貌">
        <Input.TextArea autoSize={{ minRows: 2, maxRows: 8 }} />
      </Form.Item>
      <Form.Item name="personality" label="性格">
        <Input.TextArea autoSize={{ minRows: 2, maxRows: 8 }} />
      </Form.Item>
      <Form.Item name="background" label="背景">
        <Input.TextArea autoSize={{ minRows: 2, maxRows: 10 }} />
      </Form.Item>
      <Form.Item name="profile" label="简介">
        <Input.TextArea autoSize={{ minRows: 3, maxRows: 12 }} />
      </Form.Item>
    </>
  )
}

/** 角色详情编辑故事初始状态；AI 草稿仍展示其生成的当前处境。 */
export function CharacterStateFields({ isBaseline = false }: { isBaseline?: boolean }) {
  return (
    <Form.Item
      name="current_state"
      label={isBaseline ? '初始状态' : '当前状态'}
      tooltip={isBaseline ? '故事开始时的处境、伤势和所在地；后续变化请在「剧情变化」中维护' : '角色此刻的处境、伤势、所在地等，会注入章节上下文'}
    >
      <Input.TextArea autoSize={{ minRows: 3, maxRows: 12 }} />
    </Form.Item>
  )
}
