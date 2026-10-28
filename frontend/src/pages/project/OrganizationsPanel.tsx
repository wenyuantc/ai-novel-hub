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
  Table,
  Tag,
  Tree,
  Typography,
} from 'antd'
import type { DataNode } from 'antd/es/tree'
import { useCallback, useMemo, useState, type CSSProperties } from 'react'
import { errorMessage } from '../../api/client'
import { MEMBER_STATUS, memberApi, memberStatusLabel, organizationApi } from '../../api/organizations'
import { characterApi } from '../../api/story'
import type { Member, MemberStatus, Organization, OrganizationInput } from '../../api/types'
import EntityTaskModal from '../../components/entities/EntityTaskModal'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'
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

interface MemberFormValues {
  character_id: number
  position: string
  status: MemberStatus
  note: string
}

/** 组织树 / 详情按容器宽度排布：放得下两栏（约 1:2）就并排，内容区变窄（如右侧停靠面板打开、手机）时上下堆叠。 */
const splitRow: CSSProperties = { display: 'flex', flexWrap: 'wrap', gap: 16 }
const treePane: CSSProperties = { flex: '1 1 240px', minWidth: 0 }
const detailPane: CSSProperties = { flex: '2 1 420px', minWidth: 0 }

const STATUS_OPTIONS = (Object.keys(MEMBER_STATUS) as MemberStatus[]).map((k) => ({ value: k, label: MEMBER_STATUS[k].label }))

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
  const pid = useProjectId()
  const { message } = App.useApp()
  const loader = useCallback(() => organizationApi.list(pid), [pid])
  const { data: orgs = [], loading, reload } = useLoader(loader, { projectId: pid, resources: ['organizations', 'characters'] })
  const charLoader = useCallback(() => characterApi.list(pid), [pid])
  const { data: characters = [] } = useLoader(charLoader, { projectId: pid, resources: ['organizations', 'characters'] })
  const [selectedId, setSelectedId] = useState<number | null>(null)
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

  const handleSaveOrg = async () => {
    const v = await orgForm.validateFields()
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
      const saved = editing ? await organizationApi.update(pid, editing.id, body) : await organizationApi.create(pid, body)
      message.success('已保存')
      setOrgOpen(false)
      setSelectedId(saved.id)
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const handleDeleteOrg = async (o: Organization) => {
    try {
      await organizationApi.remove(pid, o.id)
      message.success('已删除（下级组织变为顶级）')
      setSelectedId(null)
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  return (
    <Card
      title="组织"
      extra={
        <Space>
          <Button icon={<RobotOutlined />} onClick={() => setAiOpen(true)}>
            AI 生成组织
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => openOrg(null)}>
            新增组织
          </Button>
        </Space>
      }
    >
      <Spin spinning={loading}>
        {orgs.length === 0 ? (
          <Empty description="暂无组织，可手动新增或使用 AI 生成" />
        ) : (
          <div style={splitRow}>
            <div style={treePane}>
              <Card size="small" title="组织树">
                <Tree
                  treeData={tree}
                  defaultExpandAll
                  blockNode
                  selectedKeys={selectedId !== null ? [selectedId] : []}
                  onSelect={(keys) => setSelectedId(keys.length > 0 ? Number(keys[0]) : null)}
                />
              </Card>
            </div>
            <div style={detailPane}>
              {selected ? (
                <OrgDetail
                  key={selected.id}
                  projectId={pid}
                  org={selected}
                  orgs={orgs}
                  characters={characters}
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
        <Form form={orgForm} layout="vertical">
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

      <EntityTaskModal projectId={pid} kind="organization.generate" open={aiOpen} onClose={() => setAiOpen(false)} onSaved={reload} />
    </Card>
  )
}

interface DetailProps {
  projectId: number
  org: Organization
  orgs: Organization[]
  characters: { id: number; name: string }[]
  onEdit: () => void
  onDelete: () => void
  onMembersChanged: () => void
}

function OrgDetail({ projectId, org, orgs, characters, onEdit, onDelete, onMembersChanged }: DetailProps) {
  const { message } = App.useApp()
  const loader = useCallback(() => memberApi.list(projectId, org.id), [projectId, org.id])
  const { data: members = [], loading, reload } = useLoader(loader, { projectId, resources: ['organizations', 'characters'] })
  const [editing, setEditing] = useState<Member | null>(null)
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<MemberFormValues>()
  const parent = orgs.find((o) => o.id === org.parent_id)

  const openMember = (m: Member | null) => {
    setEditing(m)
    form.setFieldsValue(
      m
        ? { character_id: m.character_id, position: m.position, status: m.status as MemberStatus, note: m.note }
        : { character_id: undefined, position: '', status: 'active', note: '' },
    )
    setOpen(true)
  }

  const changed = () => {
    reload()
    onMembersChanged()
  }

  const handleSave = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      const body = { position: v.position?.trim() ?? '', status: v.status, note: v.note ?? '' }
      if (editing) await memberApi.update(projectId, org.id, editing.id, body)
      else await memberApi.create(projectId, org.id, { character_id: v.character_id, ...body })
      message.success('已保存')
      setOpen(false)
      changed()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const handleRemove = async (m: Member) => {
    try {
      await memberApi.remove(projectId, org.id, m.id)
      message.success('已移除')
      changed()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  const memberIds = new Set(members.map((m) => m.character_id))
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Card
        size="small"
        title={orgPath(orgs, org.id).join(' / ')}
        extra={
          <Space size={0}>
            <Button type="link" size="small" onClick={onEdit}>
              编辑
            </Button>
            <Popconfirm
              title="删除该组织？"
              description="成员记录一并删除，下级组织变为顶级。"
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={onDelete}
            >
              <Button type="link" size="small" danger>
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
      <Card
        size="small"
        title={`成员（${members.length}）`}
        extra={
          <Button size="small" icon={<PlusOutlined />} onClick={() => openMember(null)} disabled={characters.length === 0}>
            添加成员
          </Button>
        }
      >
        <Table<Member>
          size="small"
          rowKey="id"
          loading={loading}
          dataSource={members}
          pagination={false}
          columns={[
            { title: '角色', dataIndex: 'character_name' },
            { title: '职位', dataIndex: 'position', render: (v: string) => v || '-' },
            {
              title: '状态',
              width: 70,
              render: (_, m) => <Tag color={memberStatusLabel(m.status).color}>{memberStatusLabel(m.status).label}</Tag>,
            },
            { title: '备注', dataIndex: 'note', render: (v: string) => v || '-' },
            {
              title: '',
              width: 110,
              render: (_, m) => (
                <Space size={0}>
                  <Button type="link" size="small" onClick={() => openMember(m)}>
                    编辑
                  </Button>
                  <Popconfirm title="移出该组织？" okText="移除" cancelText="取消" onConfirm={() => handleRemove(m)}>
                    <Button type="link" size="small" danger>
                      移除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>
      <Modal
        title={editing ? '编辑成员' : '添加成员'}
        open={open}
        onOk={() => void handleSave()}
        onCancel={() => setOpen(false)}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        forceRender
      >
        <Form form={form} layout="vertical">
          <Form.Item name="character_id" label="角色" rules={[{ required: true, message: '请选择角色' }]}>
            <Select
              disabled={!!editing}
              showSearch
              optionFilterProp="label"
              options={characters.map((c) => ({ value: c.id, label: c.name, disabled: !editing && memberIds.has(c.id) }))}
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
