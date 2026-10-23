import { PlusOutlined, RobotOutlined } from '@ant-design/icons'
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
import { chapterApi } from '../../api/chapters'
import { errorMessage } from '../../api/client'
import { outlineApi } from '../../api/story'
import type { Outline, OutlineInput } from '../../api/types'
import { groupOutlines, volumeLabel } from '../../utils/outline'
import WizardModal from '../../components/WizardModal'
import { useLoader } from '../../hooks/useLoader'
import { useProjectStore } from '../../stores/project'
import { useProjectId } from '../../hooks/useProjectId'

interface PanelData {
  outlines: Outline[]
  /** outline_id -> chapter_id */
  chapterByOutline: Map<number, number>
}

export default function OutlinesPanel() {
  const pid = useProjectId()
  const { message } = App.useApp()
  const navigate = useNavigate()
  const refreshProject = useProjectStore((s) => s.refresh)
  const loader = useCallback(async (): Promise<PanelData> => {
    const [outlines, chapters] = await Promise.all([outlineApi.list(pid), chapterApi.list(pid)])
    const chapterByOutline = new Map<number, number>()
    for (const c of chapters) if (c.outline_id !== null) chapterByOutline.set(c.outline_id, c.id)
    return { outlines, chapterByOutline }
  }, [pid])
  const { data, loading, reload } = useLoader(loader)
  const outlines = useMemo(() => data?.outlines ?? [], [data])
  const chapterByOutline = data?.chapterByOutline ?? new Map<number, number>()
  const groups = useMemo(() => groupOutlines(outlines), [outlines])

  const [selected, setSelected] = useState<number[]>([])
  const [editing, setEditing] = useState<Outline | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [aiOpen, setAiOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [creatingChapters, setCreatingChapters] = useState(false)
  const [form] = Form.useForm<OutlineInput>()

  const pendingCount = outlines.filter((o) => !chapterByOutline.has(o.id)).length

  const openEdit = (item: Outline | null, volumeNo?: number) => {
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
      const last = outlines.reduce((m, o) => Math.max(m, o.chapter_no), 0)
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

  const handleDelete = async (item: Outline) => {
    try {
      await outlineApi.remove(pid, item.id)
      setSelected((s) => s.filter((id) => id !== item.id))
      message.success('已删除')
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  const createChapters = async (ids: number[] | null) => {
    setCreatingChapters(true)
    try {
      const created = await chapterApi.fromOutlines(pid, ids)
      message.success(created.length > 0 ? `已创建 ${created.length} 个章节` : '所选大纲均已建章，无需创建')
      setSelected([])
      reload()
      void refreshProject()
      if (created.length > 0) navigate(`/projects/${pid}/chapters`)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setCreatingChapters(false)
    }
  }

  return (
    <Card
      title="大纲"
      extra={
        <Space wrap>
          <Button icon={<RobotOutlined />} onClick={() => setAiOpen(true)}>
            AI 重新生成
          </Button>
          <Button icon={<PlusOutlined />} onClick={() => openEdit(null)}>
            新增大纲
          </Button>
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
        </Space>
      }
    >
      <Spin spinning={loading}>
        {outlines.length === 0 ? (
          <Empty description="暂无大纲，可手动新增或使用 AI 生成" />
        ) : (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            {groups.map((g) => (
              <Card
                key={g.volumeNo}
                size="small"
                type="inner"
                title={`${volumeLabel(g)}（${g.items.length} 章）`}
                extra={
                  <Button type="link" size="small" onClick={() => openEdit(null, g.volumeNo)}>
                    本卷新增
                  </Button>
                }
              >
                <Table<Outline>
                  size="small"
                  rowKey="id"
                  dataSource={g.items}
                  pagination={false}
                  rowSelection={{
                    selectedRowKeys: selected.filter((id) => g.items.some((o) => o.id === id)),
                    getCheckboxProps: (o) => ({ disabled: chapterByOutline.has(o.id) }),
                    onChange: (keys) => {
                      const groupIds = new Set(g.items.map((o) => o.id))
                      setSelected((prev) => [...prev.filter((id) => !groupIds.has(id)), ...keys.map(Number)])
                    },
                  }}
                  columns={[
                    { title: '章', dataIndex: 'chapter_no', width: 64 },
                    { title: '标题', dataIndex: 'title', width: 220 },
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
                      title: '章节',
                      key: 'chapter',
                      width: 90,
                      render: (_, o) => {
                        const cid = chapterByOutline.get(o.id)
                        return cid ? (
                          <Tag color="green" style={{ cursor: 'pointer' }} onClick={() => navigate(`/projects/${pid}/chapters/${cid}`)}>
                            已建章
                          </Tag>
                        ) : (
                          <Tag>未建章</Tag>
                        )
                      },
                    },
                    {
                      title: '操作',
                      key: 'actions',
                      width: 130,
                      render: (_, o) => (
                        <Space size={0}>
                          <Button type="link" size="small" onClick={() => openEdit(o)}>
                            编辑
                          </Button>
                          <Popconfirm
                            title="删除该大纲条目？"
                            description="已关联的章节不会被删除"
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
        open={aiOpen}
        existingCount={outlines.length}
        onClose={() => setAiOpen(false)}
        onDone={() => {
          setSelected([])
          reload()
        }}
      />
    </Card>
  )
}
