import { InboxOutlined, WarningOutlined } from '@ant-design/icons'
import {
  Alert,
  App,
  Button,
  Card,
  Drawer,
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
import { useSearchParams } from 'react-router-dom'
import SourceMentionFocus from '../../components/mentions/SourceMentionFocus'
import { readMentionAnchor, readPositiveId } from '../../utils/mentions'
import { kbApi } from '../../api/kb'
import type { KbDocument, KbSettings } from '../../api/types'
import IndexStatusBanner from '../../components/kb/IndexStatusBanner'
import KbChunksDrawer from '../../components/kb/KbChunksDrawer'
import KbSearchDebug from '../../components/kb/KbSearchDebug'
import RetrievalSettingsCard from '../../components/kb/RetrievalSettingsCard'
import { useIndexStatus } from '../../hooks/useIndexStatus'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore, type ProjectScope } from '../../stores/project'
import { useTasksStore } from '../../stores/tasks'
import {
  collaboratorDocStatusHint,
  collaboratorKbAccess,
  OWNER_EMBEDDING_UNAVAILABLE_REBUILD,
  ownerEmbeddingUnavailable,
  collaboratorSearchNotice,
  collaboratorUploadHint,
  COLLABORATOR_VECTOR_COST,
  docStatusHint,
  formatFileSize,
  isDocBusy,
  KB_ACCEPT,
  KB_DOC_STATUS,
  KB_MAX_DOCUMENTS,
  kbActionError,
  precheckUpload,
} from '../../utils/kb'
import { formatTime } from '../../utils/time'
import { splitPane, splitRow } from '../../utils/layout'
import { knowledgeUploadError, knowledgeUploadMode, knowledgeUploadNotice, uploadKnowledgeForScope } from './knowledgeUpload'

/** 知识库：拖拽上传、文档表格（状态 / 块数 / 注入开关 / 重建索引 / 分块 / 删除）、注入设置、检索调试、重建横幅。 */
export default function KnowledgePanel() {
  const { token } = theme.useToken()
  const pid = useProjectId()
  const [searchParams, setSearchParams] = useSearchParams()
  const mentionDocumentId = readPositiveId(searchParams.get('document_id'))
  const mentionAnchor = readMentionAnchor(searchParams)
  const { canUseAI, canUseOwnerAI, canEdit, canEditNow, role } = useProjectAccess(pid)
  const project = useProjectStore((s) => s.project)
  const user = useAuthStore((s) => s.user)
  const scope = captureProjectScope(pid)
  const uploadMode = knowledgeUploadMode(project, user, pid)
  const canUpload = canEdit && uploadMode !== 'none'
  // accepted 编辑者：向量能力只看服务端 index-status（检索模式 / 降级原因），前端不推算授权
  const collaborator = uploadMode === 'collaborator'
  const accessKey = `${project?.id === pid ? project.access?.authorization_version ?? '' : ''}:${project?.access?.allow_owner_ai ? 1 : 0}`
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
    const scope = captureProjectScope(pid)
    if (!scope) return
    try {
      const list = await kbApi.listDocuments(pid)
      if (isProjectScopeCurrent(scope)) setDocs(() => list)
    } catch {
      // 轮询失败静默；首次加载失败由 useLoader 的 error 展示
    }
  }, [pid, setDocs])

  const busy = docs.some((d) => isDocBusy(d.status))
  const { status, refresh: refreshStatus, version } = useIndexStatus(pid, {
    busy, onTick: refreshDocs, enabled: canUseOwnerAI || collaborator, foreground: collaborator, revalidateKey: accessKey,
  })
  const shared = collaboratorKbAccess(status)
  // 作者本人向量配置不可用（E6-f 裁决 18）：文档「重建索引」置灰并说明原因；协作者视图不受影响。
  const ownerUnavailable = canUseOwnerAI && ownerEmbeddingUnavailable(status)

  // 任务结束（store version 变化）后刷新文档状态
  useEffect(() => {
    void refreshDocs()
  }, [refreshDocs, version])

  const upload = async (file: File, scope: ProjectScope) => {
    setUploading((n) => n + 1)
    try {
      const result = await uploadKnowledgeForScope(pid, scope, file, track)
      if (!result) return
      const notice = knowledgeUploadNotice(result, collaborator)
      if (notice.type === 'success') message.success(notice.text)
      else message.warning(notice.text)
    } catch (err) {
      if (!isProjectScopeCurrent(scope, 'canEdit')) return
      const notice = knowledgeUploadError(err)
      if (notice.type === 'warning') message.warning(notice.text)
      else message.error(notice.text)
    } finally {
      acceptedRef.current = Math.max(0, acceptedRef.current - 1)
      setUploading((n) => n - 1)
      if (isProjectScopeCurrent(scope)) {
        void refreshDocs()
        refreshStatus()
      }
    }
  }

  const beforeUpload = (file: File) => {
    if (!canUpload || !scope || !canEditNow() || !isProjectScopeCurrent(scope, 'canEdit')) return false
    const problem = precheckUpload(file, docs.length + acceptedRef.current)
    if (problem) {
      message.error(problem)
      return false
    }
    acceptedRef.current += 1
    void upload(file, scope)
    return false
  }

  const act = async (id: number, fn: () => Promise<unknown>, ok: string) => {
    if (!canEditNow()) return
    setActing(id)
    try {
      if (!canEditNow()) return
      await fn()
      if (!canEditNow()) return
      message.success(ok)
    } catch (err) {
      if (!canEditNow()) return
      message.error(kbActionError(err))
    } finally {
      setActing(null)
      void refreshDocs()
      refreshStatus()
    }
  }

  const toggleInject = async (doc: KbDocument, inject: boolean) => {
    if (!canEditNow()) return
    setActing(doc.id)
    try {
      if (!canEditNow()) return
      const updated = await kbApi.setInject(pid, doc.id, inject)
      if (!canEditNow()) return
      setDocs((prev) => (prev ?? []).map((d) => (d.id === updated.id ? updated : d)))
    } catch (err) {
      if (!canEditNow()) return
      message.error(kbActionError(err))
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
        const hint = collaborator ? collaboratorDocStatusHint(d, status)
          : role !== 'owner' && d.status === 'ready_keyword_only'
            ? '已分块，当前仅使用关键词检索；如需向量索引，请联系作者。' : docStatusHint(d)
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
          onChange={(v) => void toggleInject(d, v)} disabled={!canEdit}
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
          {canUseOwnerAI && (ownerUnavailable ? <Tooltip title={OWNER_EMBEDDING_UNAVAILABLE_REBUILD}>
            <Button type="link" size="small" disabled>
              重建索引
            </Button>
          </Tooltip> : <Popconfirm
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
          </Popconfirm>)}
          {collaborator && canUseAI && (shared.vector ? <Popconfirm
            title="重新建索引？"
            description={`会删除现有分块，按保存的文本重新分块并生成向量；${COLLABORATOR_VECTOR_COST}。`}
            okText="重建"
            cancelText="取消"
            onConfirm={() => act(d.id, async () => track(await kbApi.reindex(pid, d.id)), '已开始重新建索引')}
          >
            <Button type="link" size="small" disabled={!canEdit || isDocBusy(d.status)}>
              重建索引
            </Button>
          </Popconfirm> : <Tooltip title={shared.known ? `不能建立向量索引：${shared.reason}` : '正在确认向量索引是否可用'}>
            <Button type="link" size="small" disabled>
              重建索引
            </Button>
          </Tooltip>)}
          <Popconfirm
            title="删除该文档？"
            description="分块与索引会一并删除"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => act(d.id, () => kbApi.remove(pid, d.id), '已删除')}
          >
            <Button type="link" size="small" danger disabled={!canEdit || (isDocBusy(d.status))}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <>
      {canUseOwnerAI && <IndexStatusBanner projectId={pid} status={status} scope="kb" onChanged={() => refreshStatus()} />}
      {collaborator && <IndexStatusBanner projectId={pid} status={status} scope="kb" audience="collaborator"
        canStart={canEditNow} onChanged={() => refreshStatus()} />}
      {canUpload && collaborator && <Alert type="info" showIcon style={{ marginBottom: 12 }}
        message={collaboratorUploadHint(status).message}
        description={collaboratorUploadHint(status).description} />}
      {settings && !settings.auto_inject && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={canUseOwnerAI ? '已关闭自动注入：生成时不会使用知识库（检索调试仍可用）' : '已关闭自动注入'}
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
            {canUpload && <Upload.Dragger
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
            {canUseOwnerAI && <KbSearchDebug projectId={pid} />}
            {collaborator && shared.authorized && <KbSearchDebug projectId={pid} notice={collaboratorSearchNotice(status)} />}
          </Space>
        </div>
      </div>
      <KbChunksDrawer projectId={pid} document={viewing} onClose={() => setViewing(null)} />
      <Drawer open={!!mentionDocumentId && !!mentionAnchor} title={docs.find((doc) => doc.id === mentionDocumentId)?.filename ?? '资料原文'} width={760} destroyOnHidden onClose={() => setSearchParams((previous) => { const next = new URLSearchParams(previous); for (const key of ['document_id', 'mention_start', 'mention_end', 'mention_hash']) next.delete(key); return next }, { replace: true })}>
        {mentionDocumentId && mentionAnchor && <SourceMentionFocus projectId={pid} type="knowledge" id={mentionDocumentId} anchor={mentionAnchor} />}
      </Drawer>
    </>
  )
}
