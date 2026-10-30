import { InboxOutlined, WarningOutlined } from '@ant-design/icons'
import {
  Alert,
  App,
  Button,
  Card,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  theme,
  Tooltip,
  Typography,
  Upload,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { duplicateDocumentId, kbApi } from '../../api/kb'
import type { KbDocument, KbSettings } from '../../api/types'
import IndexStatusBanner from '../../components/kb/IndexStatusBanner'
import KbChunksDrawer from '../../components/kb/KbChunksDrawer'
import KbSearchDebug from '../../components/kb/KbSearchDebug'
import RetrievalSettingsCard from '../../components/kb/RetrievalSettingsCard'
import { useIndexStatus } from '../../hooks/useIndexStatus'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useTasksStore } from '../../stores/tasks'
import {
  docStatusHint,
  formatFileSize,
  isDocBusy,
  KB_ACCEPT,
  KB_DOC_STATUS,
  KB_MAX_DOCUMENTS,
  precheckUpload,
} from '../../utils/kb'
import { formatTime } from '../../utils/time'
import { splitPane, splitRow } from '../../utils/layout'

/** 知识库：拖拽上传、文档表格（状态 / 块数 / 注入开关 / 重建索引 / 分块 / 删除）、注入设置、检索调试、重建横幅。 */
export default function KnowledgePanel() {
  const { token } = theme.useToken()
  const pid = useProjectId()
  const { canUseAI } = useProjectAccess(pid)
  const { message } = App.useApp()
  const track = useTasksStore((s) => s.track)

  const docsLoader = useCallback(() => kbApi.listDocuments(pid), [pid])
  const { data: docs = [], loading, error, setData: setDocs } = useLoader(docsLoader, { projectId: pid, resources: ['knowledge'] })
  const settingsLoader = useCallback(() => kbApi.getSettings(pid), [pid])
  const { data: settings, setData: setSettings } = useLoader(settingsLoader, { projectId: pid, resources: ['knowledge'] })

  const [viewing, setViewing] = useState<KbDocument | null>(null)
  const [uploading, setUploading] = useState(0)
  const [acting, setActing] = useState<number | null>(null)
  // 同一批拖入的多个文件：本批已接受的数量计入 200 上限
  const acceptedRef = useRef(0)

  /** 不显示 loading 的静默刷新（轮询 / 任务结束 / 操作后） */
  const refreshDocs = useCallback(async () => {
    try {
      const list = await kbApi.listDocuments(pid)
      setDocs(() => list)
    } catch {
      // 轮询失败静默；首次加载失败由 useLoader 的 error 展示
    }
  }, [pid, setDocs])

  const busy = docs.some((d) => isDocBusy(d.status))
  const { status, refresh: refreshStatus, version } = useIndexStatus(pid, { busy, onTick: refreshDocs, enabled: canUseAI })

  // 任务结束（store version 变化）后刷新文档状态
  useEffect(() => {
    void refreshDocs()
  }, [refreshDocs, version])

  const upload = async (file: File) => {
    setUploading((n) => n + 1)
    try {
      const r = await kbApi.upload(pid, file)
      track(r.task)
      message.success(`《${r.document.filename}》已上传，正在建索引`)
    } catch (err) {
      const dup = duplicateDocumentId(err)
      if (dup !== null) message.warning(errorMessage(err))
      else message.error(errorMessage(err))
    } finally {
      acceptedRef.current = Math.max(0, acceptedRef.current - 1)
      setUploading((n) => n - 1)
      void refreshDocs()
      refreshStatus()
    }
  }

  const beforeUpload = (file: File) => {
    if (!canUseAI) return false
    const problem = precheckUpload(file, docs.length + acceptedRef.current)
    if (problem) {
      message.error(problem)
      return false
    }
    acceptedRef.current += 1
    void upload(file)
    return false
  }

  const act = async (id: number, fn: () => Promise<unknown>, ok: string) => {
    setActing(id)
    try {
      await fn()
      message.success(ok)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setActing(null)
      void refreshDocs()
      refreshStatus()
    }
  }

  const toggleInject = async (doc: KbDocument, inject: boolean) => {
    setActing(doc.id)
    try {
      const updated = await kbApi.setInject(pid, doc.id, inject)
      setDocs((prev) => (prev ?? []).map((d) => (d.id === updated.id ? updated : d)))
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setActing(null)
    }
  }

  const columns: ColumnsType<KbDocument> = [
    {
      title: '文件',
      key: 'filename',
      render: (_, d) => (
        <Space direction="vertical" size={0}>
          <Space size={6}>
            <Typography.Text strong style={{ wordBreak: 'break-all' }}>
              {d.filename}
            </Typography.Text>
            {d.warnings.length > 0 && (
              <Tooltip title={d.warnings.join('；')}>
                <WarningOutlined style={{ color: token.colorWarning }} aria-label="解析提示" />
              </Tooltip>
            )}
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {formatFileSize(d.size)} · {d.char_count.toLocaleString()} 字
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '状态',
      key: 'status',
      width: 120,
      render: (_, d) => {
        const hint = docStatusHint(d)
        const tag = <Tag color={KB_DOC_STATUS[d.status].color}>{KB_DOC_STATUS[d.status].label}</Tag>
        return hint ? <Tooltip title={hint}>{tag}</Tooltip> : tag
      },
    },
    { title: '块数', dataIndex: 'chunk_count', width: 80 },
    {
      title: '参与注入',
      key: 'inject',
      width: 100,
      render: (_, d) => (
        <Switch
          size="small"
          checked={d.inject}
          loading={acting === d.id}
          onChange={(v) => void toggleInject(d, v)}
        />
      ),
    },
    { title: '上传时间', dataIndex: 'created_at', width: 150, render: (t: string) => formatTime(t) },
    {
      title: '操作',
      key: 'actions',
      width: 220,
      render: (_, d) => (
        <Space size={0} wrap>
          <Button type="link" size="small" onClick={() => setViewing(d)}>
            查看分块
          </Button>
          {canUseAI && <Popconfirm
            title="重新建索引？"
            description="会删除现有分块，按保存的文本重新分块并生成向量"
            okText="重建"
            cancelText="取消"
            onConfirm={() =>
              act(
                d.id,
                async () => track(await kbApi.reindex(pid, d.id)),
                '已开始重新建索引',
              )
            }
          >
            <Button type="link" size="small" disabled={isDocBusy(d.status)}>
              重建索引
            </Button>
          </Popconfirm>}
          <Popconfirm
            title="删除该文档？"
            description="分块与索引会一并删除"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => act(d.id, () => kbApi.remove(pid, d.id), '已删除')}
          >
            <Button type="link" size="small" danger disabled={isDocBusy(d.status)}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <>
      {canUseAI && <IndexStatusBanner projectId={pid} status={status} scope="kb" onChanged={() => refreshStatus()} />}
      {settings && !settings.auto_inject && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={canUseAI ? '已关闭自动注入：生成时不会使用知识库（检索调试仍可用）' : '已关闭自动注入'}
        />
      )}
      <div style={splitRow}>
        <div style={splitPane(600)}>
          <Card
            size="small"
            title={`文档（${docs.length} / ${KB_MAX_DOCUMENTS}）`}
            extra={
              <Button size="small" onClick={() => void refreshDocs()}>
                刷新
              </Button>
            }
          >
            {canUseAI && <Upload.Dragger
              multiple
              accept={KB_ACCEPT}
              showUploadList={false}
              beforeUpload={beforeUpload}
              style={{ marginBottom: 12 }}
            >
              <p className="ant-upload-drag-icon">
                <InboxOutlined />
              </p>
              <p>{uploading > 0 ? `正在上传 ${uploading} 个文件…` : '点击或拖拽文件到这里上传'}</p>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                支持 txt、md、json、docx、epub、pdf（只取文字层，不支持扫描件），单个文件 ≤ 20MB。只保存解析出的文本
              </Typography.Text>
            </Upload.Dragger>}
            {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
            <Table<KbDocument>
              rowKey="id"
              size="small"
              loading={loading}
              dataSource={docs}
              columns={columns}
              pagination={docs.length > 20 ? { pageSize: 20, showSizeChanger: false } : false}
              scroll={{ x: 760 }}
              locale={{ emptyText: '还没有上传参考资料' }}
            />
          </Card>
        </div>
        <div style={splitPane(300)}>
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <RetrievalSettingsCard
              projectId={pid}
              settings={settings}
              scope="kb"
              onSaved={(s: KbSettings) => setSettings(() => s)}
            />
            {canUseAI && <KbSearchDebug projectId={pid} />}
          </Space>
        </div>
      </div>
      <KbChunksDrawer projectId={pid} document={viewing} onClose={() => setViewing(null)} />
    </>
  )
}
