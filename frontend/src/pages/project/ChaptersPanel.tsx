import { MoreOutlined, PlusOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { App, Button, Card, Dropdown, Form, Input, List, Modal, Popconfirm, Space, Table, Tag, Tooltip, Typography } from 'antd'
import { lazy, Suspense, useCallback, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ANALYSIS_STATUS, analysisApi, formatScore } from '../../api/analysis'
import { chapterApi, CHAPTER_STATUS } from '../../api/chapters'
import { errorMessage } from '../../api/client'
import { taskApi } from '../../api/tasks'
import type { ChapterAnalysisState, ChapterBrief, ChapterStatus, Task } from '../../api/types'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useLoader } from '../../hooks/useLoader'
import { useProjectStore } from '../../stores/project'
import { useTasksStore } from '../../stores/tasks'
import { batchBannerState, queuedBatchChapterIds } from '../../utils/batchState'
import { formatTime } from '../../utils/time'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectAccess } from '../../hooks/useProjectAccess'

// 批量生成横幅 / 弹窗按需加载（章节页在入口 chunk 中）
const BatchBanner = lazy(() => import('../../components/batch/BatchBanner'))
const BatchGenerateModal = lazy(() => import('../../components/batch/BatchGenerateModal'))

export default function ChaptersPanel() {
  const pid = useProjectId()
  const { canUseAI } = useProjectAccess(pid)
  const { message, modal } = App.useApp()
  const navigate = useNavigate()
  const isMobile = useIsMobile()
  const readChapter = (c: ChapterBrief) => navigate(`/projects/${pid}/read/${c.id}`, { state: { from: `/projects/${pid}/chapters` } })
  const refreshProject = useProjectStore((s) => s.refresh)
  const loader = useCallback(() => chapterApi.list(pid), [pid])
  const { data: chapterData, loading, reload } = useLoader(loader, { projectId: pid, resources: ['chapters', 'outlines'] })
  const chapters = useMemo(() => chapterData ?? [], [chapterData])
  // 分析状态单独加载（章节列表重新加载、或有后台任务结束时刷新，如分析完成）：失败只影响「分析」列
  const tasksVersion = useTasksStore((s) => s.version)
  const statusLoader = useCallback(() => {
    void tasksVersion // 仅作为重新加载的触发依赖
    return canUseAI && chapterData ? analysisApi.statuses(pid) : Promise.resolve<ChapterAnalysisState[]>([])
  }, [pid, chapterData, tasksVersion, canUseAI])
  const { data: statuses = [] } = useLoader(statusLoader, { projectId: pid, resources: ['chapters', 'outlines'] })
  const statusById = useMemo(() => new Map(statuses.map((s) => [s.chapter_id, s])), [statuses])
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [form] = Form.useForm<{ title: string }>()
  const [batchOpen, setBatchOpen] = useState(false)
  const [dismissedBatchId, setDismissedBatchId] = useState<number | null>(null)
  const track = useTasksStore((s) => s.track)
  // 本项目未结束的批量（排队 / 运行中 / 待审核）以轮询的 store 为准；失败 / 中断的取最近一个批量任务
  const openBatch = useTasksStore((s) => s.active.find((t) => t.kind === 'chapter.batch' && t.project_id === pid))
  const latestBatchLoader = useCallback(() => {
    void tasksVersion // 有任务结束 / 本地操作时重新加载
    return canUseAI ? taskApi.list({ project_id: pid, kind: 'chapter.batch', limit: 1 }).then((list) => list[0] ?? null) : Promise.resolve(null)
  }, [pid, tasksVersion, canUseAI])
  const { data: latestBatch = null } = useLoader(latestBatchLoader, { projectId: pid, resources: ['chapters', 'outlines'] })
  const batchTask: Task | null =
    !canUseAI ? null : openBatch ??
    (latestBatch && batchBannerState(latestBatch) === 'resumable' && latestBatch.id !== dismissedBatchId
      ? latestBatch
      : null)
  const queuedIds = useMemo(() => queuedBatchChapterIds(openBatch), [openBatch])

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
      styles={isMobile ? { header: { flexWrap: 'wrap', paddingBlock: 8, gap: 8 }, body: { padding: 8 } } : undefined}
      extra={
        <Space wrap>
          {canUseAI && <Tooltip title={openBatch ? '已有进行中或待审核的批量生成' : undefined}>
            <Button
              icon={<ThunderboltOutlined />}
              disabled={chapters.length === 0 || !!openBatch}
              onClick={() => setBatchOpen(true)}
            >
              批量生成
            </Button>
          </Tooltip>}
          <Button onClick={() => navigate(`/projects/${pid}/outlines`)}>从大纲创建</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            新建章节
          </Button>
        </Space>
      }
    >
      {batchTask && (
        <Suspense fallback={null}>
          <BatchBanner
            key={batchTask.id}
            task={batchTask}
            onChaptersChanged={() => {
              reload()
              void refreshProject()
            }}
            onDismiss={() => setDismissedBatchId(batchTask.id)}
          />
        </Suspense>
      )}
      {isMobile ? (
        <List<ChapterBrief>
          loading={loading}
          dataSource={chapters}
          pagination={chapters.length > 50 ? { pageSize: 50, size: 'small' } : false}
          locale={{ emptyText: '暂无章节，可在「大纲」中按条目批量创建' }}
          renderItem={(c) => {
            const st = statusById.get(c.id)
            return (
              <List.Item
                style={{ paddingInline: 4 }}
                actions={[
                  <Dropdown
                    key="more"
                    trigger={['click']}
                    menu={{
                      items: [
                        { key: 'edit', label: '编辑' },
                        { key: 'read', label: '阅读', disabled: c.word_count === 0 },
                        { type: 'divider' },
                        { key: 'delete', label: '删除', danger: true, disabled: c.status === 'generating' },
                      ],
                      onClick: ({ key }) => {
                        if (key === 'edit') navigate(`/projects/${pid}/chapters/${c.id}`)
                        else if (key === 'read') readChapter(c)
                        else if (key === 'delete')
                          modal.confirm({
                            title: '删除该章节？',
                            content: '正文将被永久删除',
                            okText: '删除',
                            okButtonProps: { danger: true },
                            cancelText: '取消',
                            onOk: () => handleDelete(c),
                          })
                      },
                    }}
                  >
                    <Button type="text" size="small" icon={<MoreOutlined />} aria-label="更多操作" />
                  </Dropdown>,
                ]}
              >
                <div style={{ minWidth: 0, flex: 1 }} onClick={() => navigate(`/projects/${pid}/chapters/${c.id}`)}>
                  <Typography.Text strong ellipsis style={{ display: 'block' }}>
                    第 {c.chapter_no} 章 {c.title || '（未命名）'}
                  </Typography.Text>
                  <Space size={4} wrap style={{ marginTop: 4 }}>
                    <Tag color={CHAPTER_STATUS[c.status].color}>{CHAPTER_STATUS[c.status].label}</Tag>
                    {c.status !== 'generating' && queuedIds.has(c.id) && <Tag color="blue">排队批量</Tag>}
                    {st && <Tag color={ANALYSIS_STATUS[st.status].color}>{ANALYSIS_STATUS[st.status].label}</Tag>}
                    {st?.scores && <Typography.Text type="secondary">{formatScore(st.scores)} 分</Typography.Text>}
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {c.word_count} 字 · {formatTime(c.updated_at)}
                    </Typography.Text>
                  </Space>
                </div>
              </List.Item>
            )
          }}
        />
      ) : (
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
              width: 150,
              render: (s: ChapterStatus, c) => (
                <Space size={4} wrap>
                  <Tag color={CHAPTER_STATUS[s].color}>{CHAPTER_STATUS[s].label}</Tag>
                  {s !== 'generating' && queuedIds.has(c.id) && <Tag color="blue">排队批量</Tag>}
                </Space>
              ),
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
              width: 180,
              render: (_, c) => (
                <Space size={0}>
                  <Button type="link" size="small" onClick={() => navigate(`/projects/${pid}/chapters/${c.id}`)}>
                    编辑
                  </Button>
                  <Button type="link" size="small" disabled={c.word_count === 0} onClick={() => readChapter(c)}>
                    阅读
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
      )}
      {canUseAI && batchOpen && (
        <Suspense fallback={null}>
          <BatchGenerateModal
            projectId={pid}
            chapters={chapters}
            onClose={() => setBatchOpen(false)}
            onStarted={(t) => {
              setBatchOpen(false)
              track(t)
              reload()
            }}
          />
        </Suspense>
      )}
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
