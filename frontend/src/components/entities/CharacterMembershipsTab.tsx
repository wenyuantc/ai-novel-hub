import { PlusOutlined } from '@ant-design/icons'
import { App, Button, Empty, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag } from 'antd'
import { useCallback, useState } from 'react'
import { characterExtraApi } from '../../api/characters'
import { errorMessage } from '../../api/client'
import { MEMBER_STATUS, memberApi, memberStatusLabel, organizationApi } from '../../api/organizations'
import type { Character, Member, MemberStatus } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'

interface Props {
  projectId: number
  character: Character
}

interface FormValues {
  organization_id: number
  position: string
  status: MemberStatus
  note: string
}

const STATUS_OPTIONS = (Object.keys(MEMBER_STATUS) as MemberStatus[]).map((k) => ({ value: k, label: MEMBER_STATUS[k].label }))

/** 角色抽屉「组织」Tab：所属组织与职位（加入 / 修改 / 移除）。 */
export default function CharacterMembershipsTab({ projectId, character }: Props) {
  const { message } = App.useApp()
  const loader = useCallback(() => characterExtraApi.memberships(projectId, character.id), [projectId, character.id])
  const { data: rows = [], loading, reload } = useLoader(loader)
  const orgLoader = useCallback(() => organizationApi.list(projectId), [projectId])
  const { data: orgs = [] } = useLoader(orgLoader)
  const [editing, setEditing] = useState<Member | null>(null)
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<FormValues>()

  const openEdit = (m: Member | null) => {
    setEditing(m)
    form.setFieldsValue(
      m
        ? { organization_id: m.organization_id, position: m.position, status: m.status as MemberStatus, note: m.note }
        : { organization_id: undefined, position: '', status: 'active', note: '' },
    )
    setOpen(true)
  }

  const handleSave = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      if (editing) {
        await memberApi.update(projectId, editing.organization_id, editing.id, {
          position: v.position?.trim() ?? '',
          status: v.status,
          note: v.note ?? '',
        })
      } else {
        await memberApi.create(projectId, v.organization_id, {
          character_id: character.id,
          position: v.position?.trim() ?? '',
          status: v.status,
          note: v.note ?? '',
        })
      }
      message.success('已保存')
      setOpen(false)
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (m: Member) => {
    try {
      await memberApi.remove(projectId, m.organization_id, m.id)
      message.success('已移除')
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  const joined = new Set(rows.map((r) => r.organization_id))
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Button icon={<PlusOutlined />} onClick={() => openEdit(null)} disabled={orgs.length === 0}>
        加入组织
      </Button>
      <Table<Member>
        size="small"
        rowKey="id"
        loading={loading}
        dataSource={rows}
        pagination={false}
        locale={{
          emptyText: (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={orgs.length === 0 ? '项目中还没有组织' : '未加入任何组织'} />
          ),
        }}
        columns={[
          { title: '组织', dataIndex: 'organization_name' },
          { title: '职位', dataIndex: 'position', render: (v: string) => v || '-' },
          { title: '状态', width: 70, render: (_, m) => <Tag color={memberStatusLabel(m.status).color}>{memberStatusLabel(m.status).label}</Tag> },
          { title: '备注', dataIndex: 'note', render: (v: string) => v || '-' },
          {
            title: '',
            width: 110,
            render: (_, m) => (
              <Space size={0}>
                <Button type="link" size="small" onClick={() => openEdit(m)}>
                  编辑
                </Button>
                <Popconfirm title="移出该组织？" okText="移除" cancelText="取消" onConfirm={() => handleDelete(m)}>
                  <Button type="link" size="small" danger>
                    移除
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Modal
        title={editing ? '编辑成员信息' : '加入组织'}
        open={open}
        onOk={() => void handleSave()}
        onCancel={() => setOpen(false)}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        forceRender
      >
        <Form form={form} layout="vertical">
          <Form.Item name="organization_id" label="组织" rules={[{ required: true, message: '请选择组织' }]}>
            <Select
              disabled={!!editing}
              showSearch
              optionFilterProp="label"
              options={orgs.map((o) => ({ value: o.id, label: o.name, disabled: !editing && joined.has(o.id) }))}
            />
          </Form.Item>
          <Form.Item name="position" label="职位" rules={[{ max: 50, message: '不超过 50 字' }]}>
            <Input />
          </Form.Item>
          <Form.Item name="status" label="状态">
            <Select options={STATUS_OPTIONS} />
          </Form.Item>
          <Form.Item name="note" label="备注">
            <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}
