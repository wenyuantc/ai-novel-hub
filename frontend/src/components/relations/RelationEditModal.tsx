import { App, Col, Form, Input, InputNumber, Modal, Row, Select, Slider, Switch } from 'antd'
import { useEffect, useMemo, useState } from 'react'
import { errorMessage } from '../../api/client'
import {
  intimacyColor,
  isSymmetric,
  parseTypeOptionValue,
  RELATION_CATEGORY,
  RELATION_STATUS,
  relationshipApi,
  typeOptions,
  typeOptionValue,
} from '../../api/relationships'
import { asRelationStatus } from '../../api/entityTasks'
import type { Character, RelationCategory, Relationship, RelationshipType, RelationStatus } from '../../api/types'

interface Props {
  projectId: number
  open: boolean
  characters: Pick<Character, 'id' | 'name'>[]
  types: RelationshipType[]
  /** 固定视角（角色抽屉中）：「我」= 该角色 */
  viewerId?: number
  /** 编辑已有关系；省略为新建 */
  editing?: Relationship | null
  onClose: () => void
  onSaved: (r: Relationship) => void
}

interface FormValues {
  self_id: number
  other_id: number
  type_option?: string
  custom: boolean
  self_role?: string
  other_role?: string
  category: RelationCategory
  intimacy: number
  status: RelationStatus
  description: string
  chapter_no?: number | null
  note?: string
}

const STATUS_OPTIONS = (Object.keys(RELATION_STATUS) as RelationStatus[]).map((k) => ({
  value: k,
  label: RELATION_STATUS[k].label,
}))
const CATEGORY_OPTIONS = (Object.keys(RELATION_CATEGORY) as RelationCategory[]).map((k) => ({
  value: k,
  label: RELATION_CATEGORY[k].label,
}))

/**
 * 新建 / 编辑关系。类型按「我」的视角列出称呼（对方是我的徒弟 / 对方是我的师父）；
 * 新建时可改用自定义称呼（后端匹配预置 / 自定义类型，匹配不上新建）。编辑时可调换方向与类型，变化会写入历史。
 */
export default function RelationEditModal({ projectId, open, characters, types, viewerId, editing, onClose, onSaved }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  const [saving, setSaving] = useState(false)
  const selfId = Form.useWatch('self_id', form)
  const otherId = Form.useWatch('other_id', form)
  const custom = Form.useWatch('custom', form)
  const intimacy = Form.useWatch('intimacy', form) ?? 0
  const nameOf = (id: number | undefined) => characters.find((c) => c.id === id)?.name
  const selfName = viewerId !== undefined ? '我' : (nameOf(selfId) ?? '甲')
  const options = useMemo(
    () => typeOptions(types, selfName, nameOf(otherId) ?? '对方'),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [types, selfName, otherId, characters],
  )

  useEffect(() => {
    if (!open) return
    if (editing) {
      const self = viewerId ?? editing.a_id
      const other = self === editing.a_id ? editing.b_id : editing.a_id
      form.setFieldsValue({
        self_id: self,
        other_id: other,
        // 对称类型只有「a」一个选项（方向无意义）
        type_option: typeOptionValue(editing.type_id, isSymmetric(editing) || self === editing.a_id),
        custom: false,
        category: 'other',
        intimacy: editing.intimacy,
        status: asRelationStatus(editing.status),
        description: editing.description,
        chapter_no: null,
        note: '',
      })
    } else {
      form.resetFields()
      form.setFieldsValue({
        self_id: viewerId,
        custom: false,
        category: 'other',
        intimacy: 0,
        status: 'normal',
        description: '',
      })
    }
  }, [open, editing, viewerId, form])

  const handleOk = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      let saved: Relationship
      const common = {
        intimacy: v.intimacy,
        status: v.status,
        description: v.description ?? '',
        chapter_no: v.chapter_no ?? null,
        note: v.note?.trim() ?? '',
      }
      if (editing) {
        const opt = parseTypeOptionValue(v.type_option ?? '')
        if (!opt) throw new Error('请选择关系类型')
        const chosen = types.find((t) => t.id === opt.typeId)
        saved = await relationshipApi.update(projectId, editing.id, {
          ...common,
          type_id: opt.typeId,
          // 对称类型不调换方向，避免写入无意义的「方向」历史
          ...(chosen && isSymmetric(chosen) ? {} : { a_id: opt.viewerIsA ? v.self_id : v.other_id }),
        })
      } else if (v.custom) {
        // 自定义称呼：self 是 role_a 一方
        saved = await relationshipApi.create(projectId, {
          ...common,
          a_id: v.self_id,
          b_id: v.other_id,
          role_a: v.self_role?.trim(),
          role_b: v.other_role?.trim(),
          category: v.category,
          source: 'manual',
        })
      } else {
        const opt = parseTypeOptionValue(v.type_option ?? '')
        if (!opt) throw new Error('请选择关系类型')
        saved = await relationshipApi.create(projectId, {
          ...common,
          a_id: opt.viewerIsA ? v.self_id : v.other_id,
          b_id: opt.viewerIsA ? v.other_id : v.self_id,
          type_id: opt.typeId,
          source: 'manual',
        })
      }
      message.success('已保存')
      onSaved(saved)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const charOptions = (exclude?: number) =>
    characters.filter((c) => c.id !== exclude).map((c) => ({ value: c.id, label: c.name }))

  return (
    <Modal
      title={editing ? '编辑关系' : '新增关系'}
      open={open}
      onOk={() => void handleOk()}
      onCancel={onClose}
      confirmLoading={saving}
      okText="保存"
      cancelText="取消"
      width={640}
      forceRender
    >
      <Form form={form} layout="vertical">
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item name="self_id" label="角色" rules={[{ required: true, message: '请选择角色' }]} hidden={viewerId !== undefined}>
              <Select showSearch optionFilterProp="label" options={charOptions(otherId)} disabled={!!editing} />
            </Form.Item>
          </Col>
          <Col span={viewerId !== undefined ? 24 : 12}>
            <Form.Item name="other_id" label="对方" rules={[{ required: true, message: '请选择对方' }]}>
              <Select showSearch optionFilterProp="label" options={charOptions(selfId)} disabled={!!editing} />
            </Form.Item>
          </Col>
        </Row>
        {!editing && (
          <Form.Item name="custom" label="使用自定义称呼" valuePropName="checked">
            <Switch />
          </Form.Item>
        )}
        {custom && !editing ? (
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item
                name="other_role"
                label={`对方是${selfName}的`}
                rules={[{ required: true, whitespace: true, message: '请输入称呼' }, { max: 20, message: '不超过 20 字' }]}
              >
                <Input placeholder="例如：徒弟" />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                name="self_role"
                label={`${selfName}是对方的`}
                rules={[{ required: true, whitespace: true, message: '请输入称呼' }, { max: 20, message: '不超过 20 字' }]}
              >
                <Input placeholder="例如：师父" />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="category" label="类别">
                <Select options={CATEGORY_OPTIONS} />
              </Form.Item>
            </Col>
          </Row>
        ) : (
          <Form.Item name="type_option" label="关系类型" rules={[{ required: true, message: '请选择关系类型' }]}>
            <Select
              showSearch
              optionFilterProp="label"
              options={(Object.keys(RELATION_CATEGORY) as RelationCategory[])
                .map((cat) => ({
                  label: RELATION_CATEGORY[cat].label,
                  options: options.filter((o) => o.category === cat).map((o) => ({ value: o.value, label: o.label })),
                }))
                .filter((g) => g.options.length > 0)}
            />
          </Form.Item>
        )}
        <Form.Item name="intimacy" label={<span style={{ color: intimacyColor(intimacy) }}>亲密度：{intimacy}</span>}>
          <Slider min={-100} max={100} marks={{ '-100': '-100', 0: '0', 100: '100' }} />
        </Form.Item>
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item name="status" label="状态">
              <Select options={STATUS_OPTIONS} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="chapter_no" label="发生章节（可选，记入历史）">
              <InputNumber min={1} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="description" label="描述">
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} />
        </Form.Item>
        <Form.Item name="note" label="变更备注（可选，记入历史）" rules={[{ max: 200, message: '不超过 200 字' }]}>
          <Input />
        </Form.Item>
      </Form>
    </Modal>
  )
}
