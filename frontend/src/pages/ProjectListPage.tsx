import {
  BookOutlined,
  BulbOutlined,
  CloudDownloadOutlined,
  DashboardOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  FileTextOutlined,
  ImportOutlined,
  MoreOutlined,
  PlusOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Collapse,
  Dropdown,
  Empty,
  Form,
  Input,
  Modal,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Tag,
  theme,
  Typography,
} from 'antd'
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { errorMessage } from '../api/client'
import { coverUrl, PROJECT_STATUS, projectApi } from '../api/projects'
import type { Project, ProjectCreate, ProjectStatus } from '../api/types'
import ExportModal from '../components/ExportModal'
import ProjectInfoFields from '../components/ProjectInfoFields'
import BookCover from '../components/shelf/BookCover'
import StyleSelect from '../components/styles/StyleSelect'
import { useIsMobile } from '../hooks/useIsMobile'
import { useLoader } from '../hooks/useLoader'
import { formatWordCount, projectInfoBody, wordsProgress, type ProjectInfoValues } from '../utils/projectFields'
import {
  canRead,
  continueTarget,
  filterAndSort,
  readingLabel,
  readShelfPrefs,
  readTarget,
  SHELF_SORT_OPTIONS,
  shelfStats,
  writeShelfPrefs,
  type ShelfPrefs,
  type ShelfStatusFilter,
} from '../utils/shelf'
import { formatRelative, formatTime } from '../utils/time'

// 导入备份 / 备份弹窗（含 api/transfer.ts、utils/transfer.ts）按需加载，不进入口 chunk
const ImportBackupModal = lazy(() => import('../components/transfer/ImportBackupModal'))
const BackupModal = lazy(() => import('../components/transfer/BackupModal'))

const STATUS_FILTERS: { value: ShelfStatusFilter; label: string }[] = [
  { value: 'all', label: '全部状态' },
  ...(Object.keys(PROJECT_STATUS) as ProjectStatus[]).map((s) => ({ value: s, label: PROJECT_STATUS[s].label })),
]

/** 书架（/projects）：统计、搜索 / 排序 / 状态筛选（记在 localStorage）、新建卡 + 书本式项目卡片。 */
export default function ProjectListPage() {
  const { message, modal } = App.useApp()
  const navigate = useNavigate()
  const isMobile = useIsMobile()
  const loader = useCallback(() => projectApi.list(), [])
  const { data: projects = [], loading, error, reload } = useLoader(loader)
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [exportId, setExportId] = useState<number | null>(null)
  const [backupId, setBackupId] = useState<number | null>(null)
  const [prefs, setPrefs] = useState<ShelfPrefs>(readShelfPrefs)
  const [form] = Form.useForm<ProjectCreate & ProjectInfoValues>()

  useEffect(() => writeShelfPrefs(prefs), [prefs])
  const visible = useMemo(() => filterAndSort(projects, prefs), [projects, prefs])
  const stats = useMemo(() => shelfStats(projects), [projects])
  const filtering = prefs.query.trim() !== '' || prefs.status !== 'all'

  const handleCreate = async () => {
    const values = await form.validateFields()
    setCreating(true)
    try {
      const p = await projectApi.create({
        title: values.title.trim(),
        genre: values.genre?.trim() ?? '',
        premise: values.premise?.trim() ?? '',
        default_style_id: values.default_style_id ?? null,
        // 「更多信息」未展开时这些字段不在表单里，按空值处理
        ...projectInfoBody(values),
      })
      message.success('项目已创建，进入创作向导')
      setCreateOpen(false)
      form.resetFields()
      navigate(`/projects/${p.id}/wizard`)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = (p: Project) => {
    modal.confirm({
      title: `删除《${p.title}》？`,
      content: '项目下的世界观、角色、大纲和全部章节都将被删除，且无法恢复。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await projectApi.remove(p.id)
          message.success('已删除')
          reload()
        } catch (err) {
          message.error(errorMessage(err))
        }
      },
    })
  }

  const searchInput = (
    <Input
      allowClear
      prefix={<SearchOutlined />}
      placeholder="搜索书名或标签"
      value={prefs.query}
      onChange={(e) => setPrefs((p) => ({ ...p, query: e.target.value }))}
      style={{ width: isMobile ? '100%' : 240 }}
    />
  )

  return (
    <div style={{ maxWidth: 1280, margin: '0 auto', padding: isMobile ? 12 : 24 }}>
      <div
        style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12, marginBottom: 16 }}
      >
        <Typography.Title level={3} style={{ margin: 0 }}>
          我的书架
        </Typography.Title>
        <Space size={isMobile ? 'middle' : 'large'} wrap>
          <Statistic title="作品" value={stats.count} valueStyle={{ fontSize: 20 }} />
          <Statistic title="连载中" value={stats.writing} valueStyle={{ fontSize: 20 }} />
          <Statistic title="总字数" value={stats.words} valueStyle={{ fontSize: 20 }} />
        </Space>
      </div>
      {/* 小屏下搜索框独占一行、宽度 100%（不用 100vw：它包含滚动条宽度，会造成横向溢出） */}
      {isMobile && <div style={{ marginBottom: 8 }}>{searchInput}</div>}
      <Space wrap style={{ marginBottom: 16, width: '100%' }}>
        {!isMobile && searchInput}
        <Select
          value={prefs.sort}
          onChange={(sort) => setPrefs((p) => ({ ...p, sort }))}
          options={SHELF_SORT_OPTIONS}
          style={{ width: 120 }}
          aria-label="排序"
        />
        <Select
          value={prefs.status}
          onChange={(status) => setPrefs((p) => ({ ...p, status }))}
          options={STATUS_FILTERS}
          style={{ width: 120 }}
          aria-label="状态筛选"
        />
        {filtering && (
          <Typography.Text type="secondary">
            显示 {visible.length} / {projects.length}
          </Typography.Text>
        )}
      </Space>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      <Spin spinning={loading}>
        <Row gutter={[isMobile ? 12 : 20, isMobile ? 16 : 24]}>
          <Col xs={12} sm={8} md={6} xl={4}>
            <NewBookCard
              onCreate={() => setCreateOpen(true)}
              onInspire={() => navigate('/inspiration')}
              onImport={() => setImportOpen(true)}
              onBookImport={() => navigate('/book-import')}
            />
          </Col>
          {visible.map((p) => (
            <Col key={p.id} xs={12} sm={8} md={6} xl={4}>
              <BookCard
                project={p}
                onOpen={() => navigate(`/projects/${p.id}/overview`)}
                onContinue={() => navigate(continueTarget(p))}
                onRead={() => navigate(readTarget(p), { state: { from: '/projects' } })}
                onExport={() => setExportId(p.id)}
                onBackup={() => setBackupId(p.id)}
                onDelete={() => handleDelete(p)}
              />
            </Col>
          ))}
        </Row>
        {!loading && projects.length > 0 && visible.length === 0 && (
          <Empty description="没有符合条件的作品" style={{ padding: 32 }} />
        )}
        {!loading && projects.length === 0 && (
          <Typography.Paragraph type="secondary" style={{ marginTop: 16 }}>
            还没有作品：新建一个项目，或用灵感模式从一个模糊的想法开始。
          </Typography.Paragraph>
        )}
      </Spin>

      {exportId !== null && <ExportModal projectId={exportId} open onClose={() => setExportId(null)} />}
      {backupId !== null && (
        <Suspense fallback={null}>
          <BackupModal projectId={backupId} onClose={() => setBackupId(null)} />
        </Suspense>
      )}
      {importOpen && (
        <Suspense fallback={null}>
          <ImportBackupModal
            onClose={() => setImportOpen(false)}
            onImported={(p) => {
              setImportOpen(false)
              navigate(`/projects/${p.id}`)
            }}
          />
        </Suspense>
      )}
      <Modal
        title="新建项目"
        open={createOpen}
        onOk={() => void handleCreate()}
        confirmLoading={creating}
        onCancel={() => setCreateOpen(false)}
        okText="创建并进入向导"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item
            name="title"
            label="书名"
            rules={[
              { required: true, whitespace: true, message: '请输入书名' },
              { max: 200, message: '不超过 200 字' },
            ]}
          >
            <Input placeholder="例如：星河尽头" />
          </Form.Item>
          <Form.Item name="genre" label="题材" rules={[{ max: 100, message: '不超过 100 字' }]}>
            <Input placeholder="例如：玄幻 / 都市 / 科幻 / 悬疑" />
          </Form.Item>
          <Form.Item name="premise" label="一句话设定" rules={[{ max: 5000, message: '不超过 5000 字' }]}>
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} placeholder="故事的核心点子、主角与冲突" />
          </Form.Item>
          <Form.Item name="default_style_id" label="默认写作风格" extra="可在风格库中新建或从样文提取风格，之后在项目概览里修改">
            <StyleSelect placeholder="不用风格" />
          </Form.Item>
          <Collapse
            ghost
            size="small"
            items={[
              {
                key: 'more',
                label: '更多信息（简介、主题、类型标签、卖点、读者、目标字数，可稍后在项目概览里填写）',
                children: <ProjectInfoFields />,
              },
            ]}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            只有一个模糊的想法？试试 <Typography.Link onClick={() => navigate('/inspiration')}>灵感模式</Typography.Link>，让 AI 帮你一步步定下书名、简介等立项信息。
          </Typography.Text>
        </Form>
      </Modal>
    </div>
  )
}

interface NewBookCardProps {
  onCreate: () => void
  onInspire: () => void
  onImport: () => void
  onBookImport: () => void
}

/** 第一张固定的「新建」卡：新建项目 / 灵感模式 / 拆书导入（上传已有小说，/book-import）/ 导入备份。 */
function NewBookCard({ onCreate, onInspire, onImport, onBookImport }: NewBookCardProps) {
  const { token } = theme.useToken()
  return (
    <div>
      <div
        style={{
          aspectRatio: '2 / 3',
          border: `1px dashed ${token.colorBorder}`,
          borderRadius: 6,
          background: token.colorFillQuaternary,
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          gap: 8,
          padding: '0 10%',
        }}
      >
        <Button type="primary" icon={<PlusOutlined />} block onClick={onCreate}>
          新建项目
        </Button>
        <Button icon={<BulbOutlined />} block onClick={onInspire}>
          灵感模式
        </Button>
        <Button icon={<FileTextOutlined />} block onClick={onBookImport}>
          拆书导入
        </Button>
        <Button icon={<ImportOutlined />} block onClick={onImport}>
          导入备份
        </Button>
      </div>
      <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8, fontSize: 12 }}>
        开始一部新作品
      </Typography.Text>
    </div>
  )
}

interface BookCardProps {
  project: Project
  onOpen: () => void
  onContinue: () => void
  onRead: () => void
  onExport: () => void
  onBackup: () => void
  onDelete: () => void
}

/** 书本卡片：封面 + 书名 / 状态 / 前 3 个类型标签 / 字数进度 / 章节数与更新时间 + 继续写作 / 阅读 / 更多。 */
function BookCard({ project: p, onOpen, onContinue, onRead, onExport, onBackup, onDelete }: BookCardProps) {
  const progress = wordsProgress(p.total_words, p.target_words)
  const status = PROJECT_STATUS[p.status]
  return (
    <Card size="small" styles={{ body: { padding: 0 } }} variant="borderless" style={{ background: 'transparent', boxShadow: 'none' }}>
      <BookCover title={p.title} genre={p.genre} coverUrl={coverUrl(p)} onClick={onOpen} />
      <div style={{ marginTop: 8 }}>
        <Typography.Text strong ellipsis={{ tooltip: p.title }} style={{ display: 'block', cursor: 'pointer' }} onClick={onOpen}>
          {p.title}
        </Typography.Text>
        <div style={{ marginTop: 4, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
          <Tag color={status.color} style={{ marginInlineEnd: 0 }}>
            {status.label}
          </Tag>
          {(p.tags ?? []).slice(0, 3).map((t) => (
            <Tag key={t} style={{ marginInlineEnd: 0 }}>
              {t}
            </Tag>
          ))}
        </div>
        {progress !== null ? (
          <Progress
            percent={progress}
            size="small"
            style={{ margin: '4px 0 0' }}
            format={() => `${formatWordCount(p.total_words)}`}
          />
        ) : (
          <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12, marginTop: 4 }}>
            {formatWordCount(p.total_words)}
          </Typography.Text>
        )}
        <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }} title={formatTime(p.updated_at)}>
          {p.chapter_count} 章 · {formatRelative(p.updated_at)}更新
        </Typography.Text>
        <Space direction="vertical" size={6} style={{ width: '100%', marginTop: 8 }}>
          <Button type="primary" size="small" block icon={<EditOutlined />} onClick={onContinue}>
            继续写作
          </Button>
          <div style={{ display: 'flex', gap: 6 }}>
            <Button size="small" icon={<BookOutlined />} disabled={!canRead(p)} onClick={onRead} style={{ flex: 1, minWidth: 0 }}>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{readingLabel(p.reading)}</span>
            </Button>
            <Dropdown
              trigger={['click']}
              menu={{
                items: [
                  { key: 'overview', icon: <DashboardOutlined />, label: '概览' },
                  { key: 'export', icon: <DownloadOutlined />, label: '导出' },
                  { key: 'backup', icon: <CloudDownloadOutlined />, label: '备份' },
                  { type: 'divider' },
                  { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true },
                ],
                onClick: ({ key }) => {
                  if (key === 'overview') onOpen()
                  else if (key === 'export') onExport()
                  else if (key === 'backup') onBackup()
                  else if (key === 'delete') onDelete()
                },
              }}
            >
              <Button size="small" icon={<MoreOutlined />} aria-label="更多" />
            </Dropdown>
          </div>
        </Space>
      </div>
    </Card>
  )
}
