import { DownloadOutlined, EditOutlined, FileTextOutlined, KeyOutlined, PlusOutlined, ReloadOutlined, StopOutlined } from '@ant-design/icons'
import { Alert, App, Button, Skeleton, Space, Tag, Tooltip, Typography } from 'antd'
import { useState } from 'react'
import { API_KEY_STATUS, apiKeyApi, OPEN_API_DOCS_URL, OPEN_API_SCHEMA_URL } from '../api/apiKeys'
import { errorMessage } from '../api/client'
import { projectApi } from '../api/projects'
import type { ApiKey, Project } from '../api/types'
import ApiKeyFormModal from '../components/apiKeys/ApiKeyFormModal'
import ApiKeySecret from '../components/apiKeys/ApiKeySecret'
import { StudioEmpty, StudioPage, StudioPanel } from '../components/StudioUI'
import { useLoader } from '../hooks/useLoader'
import { useAuthStore } from '../stores/auth'
import { formatTime } from '../utils/time'
import './ApiKeysPage.css'

const listKeys = () => apiKeyApi.list()
const listProjects = () => projectApi.list()

function ScopeLabel({ item, projects }: { item: ApiKey; projects: Project[] }) {
  if (item.all_projects) return <span>全部自己的小说<span className="api-key-secondary">（含未来新建）</span></span>
  if (!item.project_ids.length) return <Typography.Text type="warning">没有可访问的小说，请编辑授权</Typography.Text>
  const names = item.project_ids.map((id) => projects.find((p) => p.id === id)?.title ?? `小说 #${id}`)
  return <Tooltip title={names.join('、')}><span>{names.slice(0, 3).join('、')}{names.length > 3 ? ` 等 ${names.length} 部` : ''}</span></Tooltip>
}

/** 平台访问密钥设置：默认掩码，按小说授权，提供独立 OpenAPI 文档入口。 */
function ApiKeysContent() {
  const { message, modal } = App.useApp()
  const { data, loading, error, reload, setData } = useLoader(listKeys)
  const { data: projects = [], loading: projectsLoading, error: projectsError, reload: reloadProjects } = useLoader(listProjects)
  const [editing, setEditing] = useState<{ item: ApiKey | null } | null>(null)
  const [revokingId, setRevokingId] = useState<number | null>(null)
  const [secretVersion, setSecretVersion] = useState(0)
  const keys = data ?? []

  const refresh = () => {
    setSecretVersion((value) => value + 1)
    reload()
    reloadProjects()
  }

  const revoke = (item: ApiKey) => {
    modal.confirm({
      title: `撤销密钥「${item.name}」？`,
      content: '撤销后，使用此密钥的应用将无法继续读取作品，完整密钥也无法再次查看。此操作不能恢复。',
      okText: '撤销密钥',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        // 通过行组件重新挂载立即销毁明文和未完成 reveal。
        setRevokingId(item.id)
        try {
          await apiKeyApi.revoke(item.id)
          setData((prev) => (prev ?? []).map((key) => key.id === item.id ? { ...key, status: 'revoked' } : key))
          message.success('密钥已撤销')
          reload()
        } catch (err) {
          message.error(errorMessage(err))
          throw err
        } finally {
          setRevokingId(null)
        }
      },
    })
  }

  return (
    <div className="api-keys-page">
      <StudioPage
        title="API 密钥"
        hint="让阅读器和内容工具，以只读方式连接你的作品。"
        action={
          <Space wrap>
            <Button icon={<FileTextOutlined />} href={OPEN_API_DOCS_URL} target="_blank" rel="noopener noreferrer">接口文档</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setEditing({ item: null })}>创建密钥</Button>
          </Space>
        }
      >
        <div className="api-keys-intro">
          <KeyOutlined aria-hidden />
          <div>
            <h2>作品的只读通行证</h2>
            <p>支持小说信息、章节、封面与导出，以及人物、大纲、世界观、组织和关系。平台访问密钥与模型服务商的 API Key 分别管理。</p>
            <a href={OPEN_API_SCHEMA_URL} download="ainovel-openapi.json"><DownloadOutlined aria-hidden /> 下载 OpenAPI JSON</a>
          </div>
        </div>
        {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={refresh}>重试</Button>} style={{ marginBottom: 16 }} />}
        {projectsError && <Alert type="warning" showIcon message={`小说列表加载失败：${projectsError}`} action={<Button size="small" onClick={reloadProjects}>重试</Button>} style={{ marginBottom: 16 }} />}
        <StudioPanel>
          <div className="api-keys-list-heading">
            <h2>我的密钥 <span>{keys.length}</span></h2>
            <Button icon={<ReloadOutlined />} loading={loading} onClick={refresh}>刷新</Button>
          </div>
          {loading && !data ? (
            <Skeleton active paragraph={{ rows: 4 }} />
          ) : keys.length ? (
            <div className="api-keys-list" role="list" aria-label="我的 API 密钥">
              {keys.map((item) => {
                const status = API_KEY_STATUS[item.status]
                const disabled = item.status === 'revoked' || revokingId === item.id
                return (
                  <article key={item.id} className="api-key-row" role="listitem">
                    <div className="api-key-heading">
                      <div className="api-key-name"><h3>{item.name}</h3><Tag color={status.color}>{status.label}</Tag><Tag>只读</Tag></div>
                      <Space size={4} wrap>
                        <Button size="small" icon={<EditOutlined />} disabled={disabled} onClick={() => setEditing({ item })}>编辑</Button>
                        <Button size="small" danger icon={<StopOutlined />} disabled={disabled} loading={revokingId === item.id} onClick={() => revoke(item)}>撤销</Button>
                      </Space>
                    </div>
                    <ApiKeySecret key={`${item.id}:${item.status}:${revokingId === item.id}:${secretVersion}`} item={item} disabled={disabled} />
                    <div className="api-key-scope"><span className="api-key-label">授权小说</span><ScopeLabel item={item} projects={projects} /></div>
                    <dl className="api-key-times">
                      <div><dt>创建时间</dt><dd>{formatTime(item.created_at)}</dd></div>
                      <div><dt>有效期至</dt><dd>{item.expires_at ? formatTime(item.expires_at) : '不过期'}</dd></div>
                      <div><dt>最近调用</dt><dd>{item.last_used_at ? formatTime(item.last_used_at) : '尚未调用'}</dd></div>
                    </dl>
                  </article>
                )
              })}
            </div>
          ) : !error && (
            <StudioEmpty title="还没有 API 密钥" text="为不同应用分别创建密钥，可独立管理小说授权和有效期。" action={<Button type="primary" icon={<PlusOutlined />} onClick={() => setEditing({ item: null })}>创建第一把密钥</Button>} />
          )}
        </StudioPanel>
        <p className="api-keys-note">密钥创建后仍可查看和复制，请仅交给信任的应用。网页登录登出或改密不会使密钥失效；不再使用时请主动撤销。</p>
      </StudioPage>
      {editing && <ApiKeyFormModal item={editing.item} projects={projects} projectsLoading={projectsLoading} onClose={() => setEditing(null)} onSaved={refresh} />}
    </div>
  )
}

/** 账号切换时重新挂载整个页面，避免复用前一账号的局部明文。 */
export default function ApiKeysPage() {
  const userId = useAuthStore((state) => state.user?.id)
  return userId ? <ApiKeysContent key={userId} /> : null
}
