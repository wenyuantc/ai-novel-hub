import { App, Button, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { categoryLabel, RELATION_CATEGORY, relationshipTypeApi } from '../../api/relationships'
import type { RelationCategory, RelationshipType, RelationshipTypeInput } from '../../api/types'

interface Props {
  projectId: number
  open: boolean
  types: RelationshipType[]
  onClose: () => void
  onChanged: () => void
}

const CATEGORY_OPTIONS = (Object.keys(RELATION_CATEGORY) as RelationCategory[]).map((k) => ({
  value: k,
  label: RELATION_CATEGORY[k].label,
}))

/** 关系类型管理：预置类型只读；自定义类型可新增 / 删除（被引用时后端 409）。 */
export default function RelationTypeManager({ projectId, open, types, onClose, onChanged }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<RelationshipTypeInput>()
  const [saving, setSaving] = useState(false)

  const handleAdd = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      await relationshipTypeApi.create(projectId, { category: v.category, role_a: v.role_a.trim(), role_b: v.role_b.trim() })
      message.success('已添加')
      form.resetFields()
      onChanged()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (t: RelationshipType) => {
    try {
      await relationshipTypeApi.remove(projectId, t.id)
      message.success('已删除')
      onChanged()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  return (
    <Modal open={open} title="关系类型" footer={null} width={640} onCancel={onClose}>
      <Form form={form} layout="inline" initialValues={{ category: 'other' }} style={{ marginBottom: 12, rowGap: 8 }}>
        <Form.Item name="category">
          <Select options={CATEGORY_OPTIONS} style={{ width: 90 }} />
        </Form.Item>
        <Form.Item name="role_a" rules={[{ required: true, whitespace: true, message: '必填' }, { max: 20, message: '≤20 字' }]}>
          <Input placeholder="甲的身份，如：师父" style={{ width: 140 }} />
        </Form.Item>
        <Form.Item name="role_b" rules={[{ required: true, whitespace: true, message: '必填' }, { max: 20, message: '≤20 字' }]}>
          <Input placeholder="乙的身份，如：徒弟" style={{ width: 140 }} />
        </Form.Item>
        <Button type="primary" loading={saving} onClick={() => void handleAdd()}>
          添加
        </Button>
      </Form>
      <Table<RelationshipType>
        size="small"
        rowKey="id"
        dataSource={types}
        pagination={{ pageSize: 10, hideOnSinglePage: true }}
        columns={[
          { title: '类别', width: 80, render: (_, t) => <Tag color={categoryLabel(t.category).color}>{categoryLabel(t.category).label}</Tag> },
          { title: '称呼', render: (_, t) => (t.role_a === t.role_b ? t.role_a : `${t.role_a} / ${t.role_b}`) },
          {
            title: '',
            width: 120,
            render: (_, t) =>
              t.builtin ? (
                <Tag>预置</Tag>
              ) : (
                <Space>
                  <Popconfirm title="删除该自定义类型？" okText="删除" cancelText="取消" onConfirm={() => handleDelete(t)}>
                    <Button type="link" size="small" danger>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
          },
        ]}
      />
    </Modal>
  )
}
