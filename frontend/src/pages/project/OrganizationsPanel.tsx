import { PlusOutlined, RobotOutlined } from '@ant-design/icons'
import {
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Spin,
  Tree,
  Typography,
} from 'antd'
import type { DataNode } from 'antd/es/tree'
import { useCallback, useMemo, useState } from 'react'
import { errorMessage } from '../../api/client'
import { organizationApi } from '../../api/organizations'
import type { Organization, OrganizationInput } from '../../api/types'
import EntityTaskModal from '../../components/entities/EntityTaskModal'
import { OrganizationMembershipsTab } from '../../components/entities/CharacterMembershipsTab'
import { useLoader } from '../../hooks/useLoader'
import { useEntityDeepLink } from '../../hooks/useEntityDeepLink'
import SettingEntryFieldsPanel from '../../components/settingFields/SettingEntryFieldsPanel'
import SettingTagsPanel from '../../components/settingTags/SettingTagsPanel'
import { useSettingTagsSession } from '../../components/settingTags/useSettingTagsSession'
import type { SettingTagsSession } from '../../components/settingTags/settingTagsController'
import { useSearchParams } from 'react-router-dom'
import { readEntityFieldPosition } from '../../utils/entityLinks'
import type { ProgressionPositionInput } from '../../api/types'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { splitPane, splitRow } from '../../utils/layout'
import { buildOrgTree, descendantIds, orgPath, type OrgTreeNode } from '../../utils/orgTree'

interface OrgFormValues {
  name: string
  org_type: string
  purpose: string
  location: string
  power_level: number | null
  parent_id: number | null
  description: string
  sort: number | null
}

function toTreeData(nodes: OrgTreeNode<Organization>[]): DataNode[] {
  return nodes.map((n) => ({
    key: n.org.id,
    title: (
      <span>
        {n.org.name}
        {n.org.org_type && <Typography.Text type="secondary"> · {n.org.org_type}</Typography.Text>}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {' '}
          （{n.org.member_count}）
        </Typography.Text>
      </span>
    ),
    children: toTreeData(n.children),
  }))
}

/** 组织：左侧组织树（按上级嵌套），右侧详情 + 成员表；支持 AI 生成组织。 */
export default function OrganizationsPanel() {
  const [searchParams] = useSearchParams()
  const pid = useProjectId()
  const tagsSession = useSettingTagsSession(pid)
  const { canUseAI, canEdit, canEditNow, canReadNow } = useProjectAccess(pid)
  const { message } = App.useApp()
  const loader = useCallback(() => organizationApi.list(pid), [pid])
  const { data: orgs = [], loading, reload } = useLoader(loader, { projectId: pid, resources: ['organizations', 'characters'] })
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [fieldPosition, setFieldPosition] = useState<ProgressionPositionInput | null>(null)
  const [editing, setEditing] = useState<Organization | null>(null)
  const [orgOpen, setOrgOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [aiOpen, setAiOpen] = useState(false)
  const [orgForm] = Form.useForm<OrgFormValues>()

  const tree = useMemo(() => toTreeData(buildOrgTree(orgs)), [orgs])
  const selected = orgs.find((o) => o.id === selectedId) ?? null
  const parentOptions = useMemo(() => {
    const banned = editing ? descendantIds(orgs, editing.id) : new Set<number>()
    return orgs.filter((o) => !banned.has(o.id)).map((o) => ({ value: o.id, label: orgPath(orgs, o.id).join(' / ') }))
  }, [orgs, editing])

  const openOrg = (o: Organization | null) => {
    setEditing(o)
    orgForm.setFieldsValue(
      o
        ? {
            name: o.name,
            org_type: o.org_type,
            purpose: o.purpose,
            location: o.location,
            power_level: o.power_level,
            parent_id: o.parent_id,
            description: o.description,
            sort: o.sort,
          }
        : {
            name: '',
            org_type: '',
            purpose: '',
            location: '',
            power_level: null,
            parent_id: selectedId,
            description: '',
            sort: null,
          },
    )
    setOrgOpen(true)
  }

  useEntityDeepLink(pid, orgs, loading, (org) => { setSelectedId(org.id); setFieldPosition(readEntityFieldPosition(searchParams)) }, canReadNow)

  const handleSaveOrg = async () => {
    if (!canEditNow()) return
    const v = await orgForm.validateFields()
    if (!canEditNow()) return
    const body: OrganizationInput = {
      name: v.name.trim(),
      org_type: v.org_type?.trim() ?? '',
      purpose: v.purpose ?? '',
      location: v.location?.trim() ?? '',
      // 显式 null = 清空
      power_level: v.power_level ?? null,
      parent_id: v.parent_id ?? null,
      description: v.description ?? '',
      sort: v.sort ?? undefined,
    }
    setSaving(true)
    try {
      if (!canEditNow()) return
      const saved = editing ? await organizationApi.update(pid, editing.id, body) : await organizationApi.create(pid, body)
      if (!canEditNow()) return
      message.success('已保存')
      setOrgOpen(false)
      setSelectedId(saved.id)
      reload()
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const handleDeleteOrg = async (o: Organization) => {
    if (!canEditNow()) return
    try {
      if (!canEditNow()) return
      await organizationApi.remove(pid, o.id)
      tagsSession.forget({ kind: 'organization', id: o.id })
      if (!canEditNow()) return
      message.success('已删除（下级组织变为顶级）')
      setSelectedId(null)
      reload()
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    }
  }

  return (
    <Card
      title="组织"
      extra={
        <Space>
          {canUseAI && (<Button icon={<RobotOutlined />} onClick={() => setAiOpen(true)}>
            AI 生成组织
          </Button>)}
          <Button type="primary" icon={<PlusOutlined />} onClick={() => openOrg(null)} disabled={!canEdit}>
            新增组织
          </Button>
        </Space>
      }
    >
      <Spin spinning={loading}>
        {orgs.length === 0 ? (
          <Empty description={canUseAI ? '暂无组织，可手动新增或使用 AI 生成' : '暂无组织，可手动新增'} />
        ) : (
          <div style={splitRow}>
            <div style={splitPane(240)}>
              <Card size="small" title="组织树">
                <Tree
                  treeData={tree}
                  defaultExpandAll
                  blockNode
                  selectedKeys={selectedId !== null ? [selectedId] : []}
                  onSelect={(keys) => { setSelectedId(keys.length > 0 ? Number(keys[0]) : null); setFieldPosition(null) }}
                />
              </Card>
            </div>
            <div style={splitPane(480)}>
              {selected ? (
                <OrgDetail
                  key={`${pid}:${selected.id}`}
                  projectId={pid}
                  org={selected}
                  orgs={orgs}
                  fieldPosition={fieldPosition}
                  tagsSession={tagsSession}
                  onEdit={() => openOrg(selected)}
                  onDelete={() => void handleDeleteOrg(selected)}
                  onMembersChanged={reload}
                />
              ) : (
                <Empty description="在组织树中选择一个组织查看详情与成员" />
              )}
            </div>
          </div>
        )}
      </Spin>

      <Modal
        okButtonProps={{ disabled: !canEdit }}
        title={editing ? '编辑组织' : '新增组织'}
        open={orgOpen}
        onOk={() => void handleSaveOrg()}
        onCancel={() => setOrgOpen(false)}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        width={720}
        forceRender
      >
        <Form form={orgForm} layout="vertical" disabled={!canEdit}>
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
            <Col span={12}>
              <Form.Item name="power_level" label="势力等级（0–100，可留空）">
                <InputNumber min={0} max={100} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="parent_id" label="上级组织" tooltip="不能选择自身及其下级组织">
                <Select allowClear placeholder="无（顶级）" options={parentOptions} showSearch optionFilterProp="label" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="sort" label="排序（越小越靠前，可留空）">
                <InputNumber style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="purpose" label="宗旨">
            <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea autoSize={{ minRows: 3, maxRows: 12 }} />
          </Form.Item>
        </Form>
      </Modal>

      {canUseAI && (<EntityTaskModal projectId={pid} kind="organization.generate" open={aiOpen} onClose={() => setAiOpen(false)} onSaved={reload} />)}
    </Card>
  )
}

interface DetailProps {
  tagsSession: SettingTagsSession
  fieldPosition?: ProgressionPositionInput | null
  projectId: number
  org: Organization
  orgs: Organization[]
  onEdit: () => void
  onDelete: () => void
  onMembersChanged: () => void
}

function OrgDetail({ projectId, org, orgs, onEdit, onDelete, onMembersChanged, fieldPosition, tagsSession }: DetailProps) {
  const { canEdit } = useProjectAccess(projectId)
  const parent = orgs.find((o) => o.id === org.parent_id)

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Card
        size="small"
        title={orgPath(orgs, org.id).join(' / ')}
        extra={
          <Space size={0}>
            <Button type="link" size="small" onClick={onEdit}>
              {canEdit ? '编辑' : '查看'}</Button>
            <Popconfirm
              title="删除该组织？"
              description="成员记录一并删除，下级组织变为顶级。"
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true, disabled: !canEdit }}
              onConfirm={onDelete}
            >
              <Button type="link" size="small" danger disabled={!canEdit}>
                删除
              </Button>
            </Popconfirm>
          </Space>
        }
      >
        <Descriptions size="small" column={2}>
          <Descriptions.Item label="类型">{org.org_type || '-'}</Descriptions.Item>
          <Descriptions.Item label="所在地">{org.location || '-'}</Descriptions.Item>
          <Descriptions.Item label="势力等级">{org.power_level ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="上级">{parent?.name ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="宗旨" span={2}>
            <span style={{ whiteSpace: 'pre-wrap' }}>{org.purpose || '-'}</span>
          </Descriptions.Item>
          <Descriptions.Item label="描述" span={2}>
            <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }} ellipsis={{ rows: 6, expandable: true, symbol: '展开' }}>
              {org.description || '-'}
            </Typography.Paragraph>
          </Descriptions.Item>
        </Descriptions>
      </Card>
      <Card size="small"><SettingTagsPanel session={tagsSession} entry={{ kind: 'organization', id: org.id }} /></Card>
      <Card size="small" title="组织自身字段与剧情变化">
        <SettingEntryFieldsPanel key={`${projectId}:organization:${org.id}`} projectId={projectId} entry={{ kind: 'organization', id: org.id }} initialPosition={fieldPosition} />
      </Card>
      <Card size="small" title="成员与历史">
        <OrganizationMembershipsTab projectId={projectId} organizationId={org.id} onChanged={onMembersChanged} />
      </Card>
    </Space>
  )
}
