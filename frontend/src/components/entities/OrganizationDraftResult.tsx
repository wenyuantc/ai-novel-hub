import { Alert, App, Button, Checkbox, Col, Empty, Form, Input, InputNumber, Row, Select, Space, Table, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { errorMessage } from '../../api/client'
import { parseOrganizationGenerateResult } from '../../api/entityTasks'
import { organizationApi } from '../../api/organizations'
import type { MemberSuggestion, OrganizationGenerateResult } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import type { TaskResultViewerProps } from './types'

interface OrgFormValues {
  name: string
  org_type: string
  purpose: string
  location: string
  power_level: number | null
  parent_id: number | null
  description: string
}

interface MemberRow extends MemberSuggestion {
  key: string
  checked: boolean
}

/** organization.generate 结果：可编辑草稿 + 成员建议勾选，确认后 with-members 单事务保存。 */
export default function OrganizationDraftResult({ projectId, result, onSaved }: TaskResultViewerProps) {
  const parsed = parseOrganizationGenerateResult(result)
  if (!parsed) return <Empty description="结果格式不正确，无法预览" />
  return <Editor projectId={projectId} parsed={parsed} onSaved={onSaved} />
}

function Editor({ projectId, parsed, onSaved }: { projectId: number; parsed: OrganizationGenerateResult; onSaved?: () => void }) {
  const { message } = App.useApp()
  const [form] = Form.useForm<OrgFormValues>()
  const orgLoader = useCallback(() => organizationApi.list(projectId), [projectId])
  const { data: orgs = [] } = useLoader(orgLoader)
  const [members, setMembers] = useState<MemberRow[]>(() =>
    parsed.member_suggestions.map((m, i) => ({ ...m, key: `${i}:${m.character_name}`, checked: m.resolved })),
  )
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const d = parsed.draft

  const handleSave = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      const out = await organizationApi.withMembers(projectId, {
        organization: {
          name: v.name.trim(),
          org_type: v.org_type?.trim() ?? '',
          purpose: v.purpose ?? '',
          location: v.location?.trim() ?? '',
          power_level: v.power_level ?? null,
          parent_id: v.parent_id ?? null,
          description: v.description ?? '',
        },
        members: members
          .filter((m) => m.checked && m.resolved && m.character_id !== null)
          .map((m) => ({ character_id: m.character_id as number, position: m.position.trim(), status: 'active' })),
      })
      message.success(`已保存组织「${out.organization.name}」及 ${out.members.length} 名成员`)
      setSaved(true)
      onSaved?.()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const patch = (key: string, p: Partial<MemberRow>) => setMembers((list) => list.map((m) => (m.key === key ? { ...m, ...p } : m)))
  const checked = members.filter((m) => m.checked && m.resolved).length

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {parsed.warnings.map((w) => (
        <Alert key={w} type="warning" showIcon message={w} />
      ))}
      {saved && <Alert type="success" showIcon message="已保存。" />}
      <Form
        form={form}
        layout="vertical"
        disabled={saving}
        initialValues={{
          name: d.name,
          org_type: d.org_type,
          purpose: d.purpose,
          location: d.location,
          power_level: d.power_level,
          parent_id: d.parent_id,
          description: d.description,
        }}
      >
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item
              name="name"
              label="名称"
              rules={[
                { required: true, whitespace: true, message: '请输入名称' },
                { max: 100, message: '不超过 100 字' },
              ]}
            >
              <Input />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="org_type" label="类型" rules={[{ max: 50, message: '不超过 50 字' }]}>
              <Input placeholder="例如：宗门 / 帮派 / 朝廷" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="location" label="所在地" rules={[{ max: 200, message: '不超过 200 字' }]}>
              <Input />
            </Form.Item>
          </Col>
          <Col span={6}>
            <Form.Item name="power_level" label="势力等级（0–100）">
              <InputNumber min={0} max={100} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
          <Col span={6}>
            <Form.Item name="parent_id" label="上级组织">
              <Select allowClear options={orgs.map((o) => ({ value: o.id, label: o.name }))} placeholder="无" />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="purpose" label="宗旨">
          <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} />
        </Form.Item>
        <Form.Item name="description" label="描述">
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 10 }} />
        </Form.Item>
      </Form>
      <Typography.Text strong>成员建议（已选 {checked}）</Typography.Text>
      {members.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有成员建议" />
      ) : (
        <Table<MemberRow>
          size="small"
          rowKey="key"
          pagination={false}
          dataSource={members}
          columns={[
            {
              width: 40,
              render: (_, m) => (
                <Checkbox
                  checked={m.checked && m.resolved}
                  disabled={saving || !m.resolved}
                  onChange={(e) => patch(m.key, { checked: e.target.checked })}
                />
              ),
            },
            {
              title: '角色',
              render: (_, m) => (
                <Space direction="vertical" size={0}>
                  <Typography.Text delete={!m.resolved} type={m.resolved ? undefined : 'secondary'}>
                    {m.character_name}
                  </Typography.Text>
                  {!m.resolved && (
                    <Typography.Text type="danger" style={{ fontSize: 12 }}>
                      {m.reason || '无法关联到已有角色'}
                    </Typography.Text>
                  )}
                </Space>
              ),
            },
            {
              title: '职位',
              render: (_, m) => (
                <Input
                  size="small"
                  maxLength={50}
                  value={m.position}
                  disabled={saving || !m.resolved}
                  onChange={(e) => patch(m.key, { position: e.target.value })}
                />
              ),
            },
          ]}
        />
      )}
      <Space>
        <Button type="primary" loading={saving} disabled={saved} onClick={() => void handleSave()}>
          保存组织{checked > 0 ? `及 ${checked} 名成员` : ''}
        </Button>
        <Typography.Text type="secondary">不保存则不写入任何数据。</Typography.Text>
      </Space>
    </Space>
  )
}
