import { DownloadOutlined, PlusOutlined, RobotOutlined, UploadOutlined } from '@ant-design/icons'
import {
  App,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Popconfirm,
  Row,
  Segmented,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import { useCallback, useMemo, useState } from 'react'
import { lifeStatusLabel, roleTypeColor } from '../../api/characters'
import { errorMessage } from '../../api/client'
import { characterApi } from '../../api/story'
import type { Character } from '../../api/types'
import CardExportModal from '../../components/entities/CardExportModal'
import CardImportModal from '../../components/entities/CardImportModal'
import { characterToForm, EMPTY_CHARACTER, formToCharacterInput, type CharacterFormValues } from '../../components/entities/characterForm'
import { CharacterBasicFields, CharacterDescFields, CharacterStateFields } from '../../components/entities/CharacterFormFields'
import CharacterMembershipsTab from '../../components/entities/CharacterMembershipsTab'
import CharacterRelationsTab from '../../components/entities/CharacterRelationsTab'
import EntityTaskModal from '../../components/entities/EntityTaskModal'
import WizardModal from '../../components/WizardModal'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectStore } from '../../stores/project'

const ALL = '__all__'

/** 角色：卡片列表（按定位筛选、勾选导出）+ 分 Tab 编辑抽屉（基础 / 描写 / 状态 / 关系 / 组织）。 */
export default function CharactersPanel() {
  const pid = useProjectId()
  const { message } = App.useApp()
  const projectTitle = useProjectStore((s) => s.project?.title ?? '')
  const loader = useCallback(() => characterApi.list(pid), [pid])
  const { data: items = [], loading, reload } = useLoader(loader)
  const [editing, setEditing] = useState<Character | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [tab, setTab] = useState('basic')
  const [wizardOpen, setWizardOpen] = useState(false)
  const [generateOpen, setGenerateOpen] = useState(false)
  const [exportOpen, setExportOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [filter, setFilter] = useState<string>(ALL)
  const [selected, setSelected] = useState<number[]>([])
  const [form] = Form.useForm<CharacterFormValues>()

  const roleTypes = useMemo(() => {
    const set = new Set(items.map((c) => c.role_type.trim() || '未设定'))
    return [...set]
  }, [items])
  const shown = filter === ALL ? items : items.filter((c) => (c.role_type.trim() || '未设定') === filter)
  // 只保留仍存在的勾选
  const selectedIds = useMemo(() => selected.filter((id) => items.some((c) => c.id === id)), [selected, items])

  const openEdit = (item: Character | null) => {
    setEditing(item)
    setTab('basic')
    form.setFieldsValue(item ? characterToForm(item) : EMPTY_CHARACTER)
    setDrawerOpen(true)
  }

  const handleSave = async () => {
    let v: CharacterFormValues
    try {
      v = await form.validateFields()
    } catch {
      message.warning('请检查表单中的必填项')
      return
    }
    const body = formToCharacterInput(v)
    setSaving(true)
    try {
      const saved = editing ? await characterApi.update(pid, editing.id, body) : await characterApi.create(pid, body)
      message.success('已保存')
      setEditing(saved)
      form.setFieldsValue(characterToForm(saved))
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (item: Character) => {
    try {
      await characterApi.remove(pid, item.id)
      message.success('已删除（相关关系与组织成员记录一并清理）')
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  const toggle = (id: number, on: boolean) =>
    setSelected((list) => (on ? [...list.filter((x) => x !== id), id] : list.filter((x) => x !== id)))

  return (
    <Card
      title="角色"
      extra={
        <Space wrap>
          <Button icon={<RobotOutlined />} onClick={() => setGenerateOpen(true)}>
            AI 生成角色
          </Button>
          <Button icon={<DownloadOutlined />} onClick={() => setExportOpen(true)} disabled={items.length === 0}>
            导出卡片{selectedIds.length > 0 ? `（${selectedIds.length}）` : ''}
          </Button>
          <Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>
            导入卡片
          </Button>
          <Button onClick={() => setWizardOpen(true)}>AI 重新生成全部</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => openEdit(null)}>
            新增角色
          </Button>
        </Space>
      }
    >
      <Spin spinning={loading}>
        {items.length === 0 ? (
          <Empty description="暂无角色，可手动新增、导入卡片或使用 AI 生成" />
        ) : (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Space wrap>
              <Segmented
                value={filter}
                onChange={(v) => setFilter(String(v))}
                options={[
                  { value: ALL, label: `全部（${items.length}）` },
                  ...roleTypes.map((r) => ({ value: r, label: r })),
                ]}
              />
              <Checkbox
                checked={shown.length > 0 && shown.every((c) => selectedIds.includes(c.id))}
                indeterminate={shown.some((c) => selectedIds.includes(c.id)) && !shown.every((c) => selectedIds.includes(c.id))}
                onChange={(e) => {
                  const ids = shown.map((c) => c.id)
                  setSelected((list) =>
                    e.target.checked ? [...new Set([...list, ...ids])] : list.filter((x) => !ids.includes(x)),
                  )
                }}
              >
                全选当前
              </Checkbox>
            </Space>
            <Row gutter={[16, 16]}>
              {shown.map((c) => {
                const life = lifeStatusLabel(c.life_status)
                return (
                  <Col key={c.id} xs={24} md={12} xl={8}>
                    <Card
                      size="small"
                      title={
                        <Space size={4} wrap>
                          <Checkbox checked={selectedIds.includes(c.id)} onChange={(e) => toggle(c.id, e.target.checked)} />
                          {c.name}
                          {c.role_type && <Tag color={roleTypeColor(c.role_type)}>{c.role_type}</Tag>}
                          {c.life_status !== 'alive' && <Tag color={life.color}>{life.label}</Tag>}
                          {c.always_in_context && <Tag color="geekblue">常驻上下文</Tag>}
                        </Space>
                      }
                      extra={
                        <Space size={0}>
                          <Button type="link" size="small" onClick={() => openEdit(c)}>
                            编辑
                          </Button>
                          <Popconfirm
                            title="删除该角色？"
                            description="相关的人物关系、关系历史、组织成员记录会一并删除。"
                            okText="删除"
                            cancelText="取消"
                            okButtonProps={{ danger: true }}
                            onConfirm={() => handleDelete(c)}
                          >
                            <Button type="link" size="small" danger>
                              删除
                            </Button>
                          </Popconfirm>
                        </Space>
                      }
                    >
                      <Descriptions size="small" column={1}>
                        {c.aliases.length > 0 && <Descriptions.Item label="别名">{c.aliases.join('、')}</Descriptions.Item>}
                        {(c.age || c.gender) && (
                          <Descriptions.Item label="年龄 / 性别">{[c.age, c.gender].filter(Boolean).join(' / ')}</Descriptions.Item>
                        )}
                        <Descriptions.Item label="简介">
                          <Typography.Paragraph
                            style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}
                            ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}
                          >
                            {c.profile || c.personality || c.background || '-'}
                          </Typography.Paragraph>
                        </Descriptions.Item>
                        {c.current_state && (
                          <Descriptions.Item label="当前状态">
                            <Typography.Paragraph style={{ marginBottom: 0 }} ellipsis={{ rows: 2 }}>
                              {c.current_state}
                            </Typography.Paragraph>
                          </Descriptions.Item>
                        )}
                      </Descriptions>
                    </Card>
                  </Col>
                )
              })}
            </Row>
          </Space>
        )}
      </Spin>

      <Drawer
        title={editing ? `编辑角色：${editing.name}` : '新增角色'}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={760}
        forceRender
        extra={
          <Button type="primary" loading={saving} onClick={() => void handleSave()}>
            保存
          </Button>
        }
      >
        <Form form={form} layout="vertical">
          <Tabs
            activeKey={tab}
            onChange={setTab}
            items={[
              { key: 'basic', label: '基础', forceRender: true, children: <CharacterBasicFields form={form} creating={!editing} /> },
              { key: 'desc', label: '描写', forceRender: true, children: <CharacterDescFields /> },
              { key: 'state', label: '状态', forceRender: true, children: <CharacterStateFields /> },
              ...(editing
                ? [
                    {
                      key: 'rel',
                      label: '关系',
                      children: <CharacterRelationsTab projectId={pid} character={editing} characters={items} />,
                    },
                    {
                      key: 'org',
                      label: '组织',
                      children: <CharacterMembershipsTab projectId={pid} character={editing} />,
                    },
                  ]
                : []),
            ]}
          />
        </Form>
        {!editing && (
          <Typography.Text type="secondary">保存后可在「关系」「组织」Tab 中设置人物关系与所属组织。</Typography.Text>
        )}
      </Drawer>

      <WizardModal
        projectId={pid}
        kind="characters"
        open={wizardOpen}
        existingCount={items.length}
        onClose={() => setWizardOpen(false)}
        onDone={() => reload()}
      />
      <EntityTaskModal
        projectId={pid}
        kind="character.generate"
        open={generateOpen}
        onClose={() => setGenerateOpen(false)}
        onSaved={reload}
      />
      <CardExportModal
        projectId={pid}
        projectTitle={projectTitle}
        open={exportOpen}
        characters={items}
        initialCharacterIds={selectedIds}
        onClose={() => setExportOpen(false)}
      />
      <CardImportModal projectId={pid} open={importOpen} onClose={() => setImportOpen(false)} onImported={reload} />
    </Card>
  )
}
