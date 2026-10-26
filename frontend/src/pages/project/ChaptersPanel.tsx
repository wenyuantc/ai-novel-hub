import { PlusOutlined } from '@ant-design/icons'
import { App, Button, Card, Form, Input, Modal, Popconfirm, Space, Table, Tag } from 'antd'
import { useCallback, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ANALYSIS_STATUS, analysisApi, formatScore } from '../../api/analysis'
import { chapterApi, CHAPTER_STATUS } from '../../api/chapters'
import { errorMessage } from '../../api/client'
import type { ChapterAnalysisState, ChapterBrief, ChapterStatus } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useProjectStore } from '../../stores/project'
import { useTasksStore } from '../../stores/tasks'
import { formatTime } from '../../utils/time'
import { useProjectId } from '../../hooks/useProjectId'

export default function ChaptersPanel() {
  const pid = useProjectId()
  const { message } = App.useApp()
  const navigate = useNavigate()
  const refreshProject = useProjectStore((s) => s.refresh)
  const loader = useCallback(() => chapterApi.list(pid), [pid])
  const { data: chapterData, loading, reload } = useLoader(loader)
  const chapters = useMemo(() => chapterData ?? [], [chapterData])
  // 分析状态单独加载（章节列表重新加载、或有后台任务结束时刷新，如分析完成）：失败只影响「分析」列
  const tasksVersion = useTasksStore((s) => s.version)
  const statusLoader = useCallback(() => {
    void tasksVersion // 仅作为重新加载的触发依赖
    return chapterData ? analysisApi.statuses(pid) : Promise.resolve<ChapterAnalysisState[]>([])
  }, [pid, chapterData, tasksVersion])
  const { data: statuses = [] } = useLoader(statusLoader)
  const statusById = useMemo(() => new Map(statuses.map((s) => [s.chapter_id, s])), [statuses])
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [form] = Form.useForm<{ title: string }>()

  const totalWords = chapters.reduce((s, c) => s + c.word_count, 0)

  const handleCreate = async () => {
    const v = await form.validateFields()
    setCreating(true)
    try {
      const c = await chapterApi.create(pid, { title: v.title?.trim() || null })
      setCreateOpen(false)
      form.resetFields()
      void refreshProject()
      navigate(`/projects/${pid}/chapters/${c.id}`)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = async (c: ChapterBrief) => {
    try {
      await chapterApi.remove(pid, c.id)
      message.success('已删除')
      reload()
      void refreshProject()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  return (
    <Card
      title={`章节（共 ${chapters.length} 章 · ${totalWords} 字）`}
      extra={
        <Space>
          <Button onClick={() => navigate(`/projects/${pid}/outlines`)}>从大纲创建</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            新建章节
          </Button>
        </Space>
      }
    >
      <Table<ChapterBrief>
        rowKey="id"
        loading={loading}
        dataSource={chapters}
        pagination={{ pageSize: 50, hideOnSinglePage: true }}
        onRow={(c) => ({ onDoubleClick: () => navigate(`/projects/${pid}/chapters/${c.id}`) })}
        locale={{ emptyText: '暂无章节，可在「大纲」中按条目批量创建' }}
        columns={[
          { title: '章', dataIndex: 'chapter_no', width: 70 },
          {
            title: '标题',
            dataIndex: 'title',
            render: (t: string, c) => (
              <Link to={`/projects/${pid}/chapters/${c.id}`}>{t || '（未命名）'}</Link>
            ),
          },
          {
            title: '状态',
            dataIndex: 'status',
            width: 100,
            render: (s: ChapterStatus) => <Tag color={CHAPTER_STATUS[s].color}>{CHAPTER_STATUS[s].label}</Tag>,
          },
          {
            title: '分析',
            key: 'analysis',
            width: 130,
            render: (_, c) => {
              const st = statusById.get(c.id)
              if (!st) return null
              const label = ANALYSIS_STATUS[st.status]
              return (
                <Space size={4}>
                  <Tag color={label.color}>{label.label}</Tag>
                  {st.scores && <span title="整体评分">{formatScore(st.scores)}</span>}
                </Space>
              )
            },
          },
          { title: '字数', dataIndex: 'word_count', width: 90 },
          { title: '更新时间', dataIndex: 'updated_at', width: 160, render: (t: string) => formatTime(t) },
          {
            title: '操作',
            key: 'actions',
            width: 140,
            render: (_, c) => (
              <Space size={0}>
                <Button type="link" size="small" onClick={() => navigate(`/projects/${pid}/chapters/${c.id}`)}>
                  编辑
                </Button>
                <Popconfirm
                  title="删除该章节？"
                  description="正文将被永久删除"
                  okText="删除"
                  cancelText="取消"
                  okButtonProps={{ danger: true }}
                  onConfirm={() => handleDelete(c)}
                >
                  <Button type="link" size="small" danger disabled={c.status === 'generating'}>
                    删除
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Modal
        title="新建章节"
        open={createOpen}
        onOk={() => void handleCreate()}
        onCancel={() => setCreateOpen(false)}
        confirmLoading={creating}
        okText="创建"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item name="title" label="章节标题（可留空）" rules={[{ max: 200, message: '不超过 200 字' }]}>
            <Input placeholder="例如：风起青萍" />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
