import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DownloadOutlined,
  HistoryOutlined,
  PlusOutlined,
  RobotOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import {
  App,
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from 'antd'
import { useCallback, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { downloadJson } from '../../api/cards'
import { chapterApi } from '../../api/chapters'
import { errorMessage } from '../../api/client'
import {
  canBuild,
  canCreateSingleChapter,
  chapterRange,
  expandStatus,
  outlineFilename,
  outlineProApi,
} from '../../api/outlines'
import { characterApi, outlineApi } from '../../api/story'
import type { OutlineDetail, OutlineInput, OutlineMoveDirection } from '../../api/types'
import OutlineImportModal from '../../components/outlines/OutlineImportModal'
import OutlineSnapshotDrawer from '../../components/outlines/OutlineSnapshotDrawer'
import OutlineTaskModal, { type OutlineTaskKind } from '../../components/outlines/OutlineTaskModal'
import PlanList from '../../components/outlines/PlanList'
import WizardModal from '../../components/WizardModal'
import { usePublishAgentContext } from '../../hooks/useAgentPageContext'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectStore } from '../../stores/project'
import { groupOutlines, volumeLabel } from '../../utils/outline'

/**
 * 大纲页（懒加载路由）：按卷分组的章纲 + 展开状态；已展开的大纲可折叠显示细纲（行内编辑）；
 * 上移 / 下移（只改排列顺序）、展开、细纲建章；工具栏 AI 续写、导入 / 导出、快照、向导重新生成、1 纲 1 章批量建章。
 */
export default function OutlinesPanel() {
  const pid = useProjectId()
  const { message } = App.useApp()
  const navigate = useNavigate()
  const refreshProject = useProjectStore((s) => s.refresh)
  const projectTitle = useProjectStore((s) => s.project?.title ?? '')
  const loader = useCallback(() => Promise.all([outlineProApi.tree(pid), characterApi.list(pid)]), [pid])
  const { data, loading, reload } = useLoader(loader, { projectId: pid, resources: ['outlines', 'characters', 'chapters'] })
  const outlines = useMemo(() => data?.[0] ?? [], [data])
  const characters = useMemo(() => data?.[1] ?? [], [data])
  const groups = useMemo(() => groupOutlines(outlines), [outlines])
  const characterOptions = useMemo(() => characters.map((c) => ({ value: c.id, label: c.name })), [characters])
  const characterNames = useMemo(() => new Map(characters.map((c) => [c.id, c.name])), [characters])

  const [selected, setSelected] = useState<number[]>([])
  const [expandedKeys, setExpandedKeys] = useState<number[]>([])
  const [editing, setEditing] = useState<OutlineDetail | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [wizardOpen, setWizardOpen] = useState(false)
  const [taskKind, setTaskKind] = useState<OutlineTaskKind | null>(null)
  const [expandTarget, setExpandTarget] = useState<OutlineDetail | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [snapshotsOpen, setSnapshotsOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [creatingChapters, setCreatingChapters] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [pending, setPending] = useState<number | null>(null)
  const [form] = Form.useForm<OutlineInput>()
  usePublishAgentContext(pid, { outline_id: modalOpen ? editing?.id : expandedKeys.at(-1) })

  const pendingCount = outlines.filter(canCreateSingleChapter).length

  /** 数据变化后刷新大纲与项目统计 */
  const afterChange = () => {
    reload()
    void refreshProject()
  }

  const openEdit = (item: OutlineDetail | null, volumeNo?: number) => {
    setEditing(item)
    if (item) {
      form.setFieldsValue({
        volume_no: item.volume_no,
        volume_title: item.volume_title,
        chapter_no: item.chapter_no,
        title: item.title,
        summary: item.summary,
      })
    } else {
      let last = 0
      for (const o of outlines) {
        last = Math.max(last, o.chapter_no)
        for (const p of o.plans) if (p.chapter_no !== null) last = Math.max(last, p.chapter_no)
      }
      const vol = volumeNo ?? groups.at(-1)?.volumeNo ?? 1
      form.setFieldsValue({
        volume_no: vol,
        volume_title: groups.find((g) => g.volumeNo === vol)?.volumeTitle ?? '',
        chapter_no: last + 1,
        title: '',
        summary: '',
      })
    }
    setModalOpen(true)
  }

  const handleSave = async () => {
    const v = await form.validateFields()
    const body: OutlineInput = {
      volume_no: v.volume_no,
      volume_title: v.volume_title?.trim() ?? '',
      chapter_no: v.chapter_no,
      title: v.title.trim(),
      summary: v.summary ?? '',
    }
    setSaving(true)
    try {
      if (editing) await outlineApi.update(pid, editing.id, body)
      else await outlineApi.create(pid, body)
      message.success('已保存')
      setModalOpen(false)
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (item: OutlineDetail) => {
    try {
      await outlineApi.remove(pid, item.id)
      setSelected((s) => s.filter((id) => id !== item.id))
      message.success('已删除')
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  const handleMove = async (item: OutlineDetail, direction: OutlineMoveDirection) => {
    setPending(item.id)
    try {
      await outlineProApi.move(pid, item.id, direction)
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setPending(null)
    }
  }

  const handleBuild = async (item: OutlineDetail) => {
    setPending(item.id)
    try {
      const r = await outlineProApi.build(pid, item.id)
      message.success(`已为细纲建章 ${r.chapters.length} 个，后续章节号已顺延`)
      setExpandedKeys((keys) => (keys.includes(item.id) ? keys : [...keys, item.id]))
      afterChange()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setPending(null)
    }
  }

  const createChapters = async (ids: number[] | null) => {
    setCreatingChapters(true)
    try {
      const created = await chapterApi.fromOutlines(pid, ids)
      message.success(created.length > 0 ? `已创建 ${created.length} 个章节` : '所选大纲均已建章或已展开，无需创建')
      setSelected([])
      afterChange()
      if (created.length > 0) navigate(`/projects/${pid}/chapters`)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setCreatingChapters(false)
    }
  }

  const handleExport = async () => {
    setExporting(true)
    try {
      const fileData = await outlineProApi.export(pid)
      const name = outlineFilename(projectTitle)
      downloadJson(fileData, name)
      message.success(`已导出：${name}`)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setExporting(false)
    }
  }

  const openExpand = (o: OutlineDetail) => {
    setExpandTarget(o)
    setTaskKind('outline.expand')
  }

  return (
    <Card
      title="大纲"
      extra={
        <Space wrap>
          <Button icon={<RobotOutlined />} onClick={() => setTaskKind('outline.continue')}>
            AI 续写
          </Button>
          <Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>
            导入
          </Button>
          <Button icon={<DownloadOutlined />} loading={exporting} disabled={outlines.length === 0} onClick={() => void handleExport()}>
            导出
          </Button>
          <Button icon={<HistoryOutlined />} onClick={() => setSnapshotsOpen(true)}>
            快照
          </Button>
          <Button onClick={() => setWizardOpen(true)}>AI 重新生成</Button>
          <Button icon={<PlusOutlined />} onClick={() => openEdit(null)}>
            新增大纲
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Space wrap>
          <Button
            type="primary"
            disabled={selected.length === 0}
            loading={creatingChapters}
            onClick={() => void createChapters(selected)}
          >
            为选中条目创建章节{selected.length > 0 ? `（${selected.length}）` : ''}
          </Button>
          <Button disabled={pendingCount === 0} loading={creatingChapters} onClick={() => void createChapters(null)}>
            全部未建章条目创建章节
          </Button>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            未展开的大纲按 1 纲 1 章建章；已展开的大纲请在该行「建章」按细纲创建。
          </Typography.Text>
        </Space>
        <Spin spinning={loading}>
          {outlines.length === 0 ? (
            <Empty description="暂无大纲，可手动新增、导入或使用 AI 生成" />
          ) : (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              {groups.map((g) => (
                <Card
                  key={g.volumeNo}
                  size="small"
                  type="inner"
                  title={`${volumeLabel(g)}（${g.items.length} 条章纲）`}
                  extra={
                    <Button type="link" size="small" onClick={() => openEdit(null, g.volumeNo)}>
                      本卷新增
                    </Button>
                  }
                >
                  <Table<OutlineDetail>
                    size="small"
                    rowKey="id"
                    dataSource={g.items}
                    pagination={false}
                    expandable={{
                      expandedRowKeys: expandedKeys.filter((k) => g.items.some((o) => o.id === k)),
                      rowExpandable: (o) => o.plan_count > 0,
                      onExpand: (open, o) =>
                        setExpandedKeys((keys) => (open ? [...keys, o.id] : keys.filter((k) => k !== o.id))),
                      expandedRowRender: (o) => (
                        <PlanList
                          projectId={pid}
                          outline={o}
                          characterOptions={characterOptions}
                          characterNames={characterNames}
                          onChanged={afterChange}
                        />
                      ),
                    }}
                    rowSelection={{
                      selectedRowKeys: selected.filter((id) => g.items.some((o) => o.id === id)),
                      getCheckboxProps: (o) => ({ disabled: !canCreateSingleChapter(o) }),
                      onChange: (keys) => {
                        const groupIds = new Set(g.items.map((o) => o.id))
                        setSelected((prev) => [...prev.filter((id) => !groupIds.has(id)), ...keys.map(Number)])
                      },
                    }}
                    columns={[
                      { title: '章', key: 'no', width: 72, render: (_, o) => chapterRange(o) },
                      { title: '标题', dataIndex: 'title', width: 200 },
                      {
                        title: '梗概',
                        dataIndex: 'summary',
                        render: (s: string) => (
                          <Typography.Paragraph
                            style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}
                            ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}
                          >
                            {s || '-'}
                          </Typography.Paragraph>
                        ),
                      },
                      {
                        title: '状态',
                        key: 'status',
                        width: 150,
                        render: (_, o) => {
                          const st = expandStatus(o)
                          return o.plan_count === 0 && o.chapter_id !== null ? (
                            <Tag
                              color={st.color}
                              style={{ cursor: 'pointer' }}
                              onClick={() => navigate(`/projects/${pid}/chapters/${o.chapter_id}`)}
                            >
                              {st.label}
                            </Tag>
                          ) : (
                            <Tag color={st.color}>{st.label}</Tag>
                          )
                        },
                      },
                      {
                        title: '操作',
                        key: 'actions',
                        width: 270,
                        render: (_, o, index) => (
                          <Space size={0} wrap>
                            <Button type="link" size="small" onClick={() => openEdit(o)}>
                              编辑
                            </Button>
                            <Button type="link" size="small" onClick={() => openExpand(o)}>
                              {o.plan_count > 0 ? '重新展开' : '展开'}
                            </Button>
                            {canBuild(o) && (
                              <Button type="link" size="small" loading={pending === o.id} onClick={() => void handleBuild(o)}>
                                建章
                              </Button>
                            )}
                            <Button
                              type="text"
                              size="small"
                              title="上移"
                              icon={<ArrowUpOutlined />}
                              disabled={index === 0 || pending !== null}
                              onClick={() => void handleMove(o, 'up')}
                            />
                            <Button
                              type="text"
                              size="small"
                              title="下移"
                              icon={<ArrowDownOutlined />}
                              disabled={index === g.items.length - 1 || pending !== null}
                              onClick={() => void handleMove(o, 'down')}
                            />
                            <Popconfirm
                              title="删除该大纲条目？"
                              description={
                                o.plan_count > 0 ? '细纲会一并删除；已建的章节不会删除，只解除关联' : '已关联的章节不会被删除'
                              }
                              okText="删除"
                              cancelText="取消"
                              okButtonProps={{ danger: true }}
                              onConfirm={() => handleDelete(o)}
                            >
                              <Button type="link" size="small" danger>
                                删除
                              </Button>
                            </Popconfirm>
                          </Space>
                        ),
                      },
                    ]}
                  />
                </Card>
              ))}
            </Space>
          )}
        </Spin>
      </Space>

      <Modal
        title={editing ? '编辑大纲条目' : '新增大纲条目'}
        open={modalOpen}
        onOk={() => void handleSave()}
        onCancel={() => setModalOpen(false)}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        width={720}
        forceRender
      >
        <Form form={form} layout="vertical">
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item name="volume_no" label="卷号" rules={[{ required: true, message: '请输入卷号' }]}>
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="volume_title" label="卷名" rules={[{ max: 200, message: '不超过 200 字' }]}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="chapter_no" label="章号" rules={[{ required: true, message: '请输入章号' }]}>
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            name="title"
            label="章节标题"
            rules={[
              { required: true, whitespace: true, message: '请输入标题' },
              { max: 200, message: '不超过 200 字' },
            ]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="summary" label="章节梗概">
            <Input.TextArea autoSize={{ minRows: 4, maxRows: 14 }} />
          </Form.Item>
        </Form>
      </Modal>

      <WizardModal
        projectId={pid}
        kind="outline"
        open={wizardOpen}
        existingCount={outlines.length}
        onClose={() => setWizardOpen(false)}
        onDone={() => {
          setSelected([])
          reload()
        }}
      />
      <OutlineTaskModal
        projectId={pid}
        kind={taskKind ?? 'outline.continue'}
        open={taskKind !== null}
        outline={taskKind === 'outline.expand' ? expandTarget : null}
        onClose={() => {
          setTaskKind(null)
          setExpandTarget(null)
        }}
        onSaved={() => {
          if (taskKind === 'outline.expand' && expandTarget) {
            const id = expandTarget.id
            setExpandedKeys((keys) => (keys.includes(id) ? keys : [...keys, id]))
          }
          afterChange()
        }}
      />
      <OutlineImportModal
        projectId={pid}
        open={importOpen}
        existingCount={outlines.length}
        onClose={() => setImportOpen(false)}
        onImported={() => {
          setSelected([])
          afterChange()
        }}
      />
      <OutlineSnapshotDrawer
        projectId={pid}
        open={snapshotsOpen}
        onClose={() => setSnapshotsOpen(false)}
        onRestored={() => {
          setSelected([])
          afterChange()
        }}
      />
    </Card>
  )
}
