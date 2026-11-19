import { RobotOutlined, TeamOutlined } from '@ant-design/icons'
import { Alert, Button, Drawer, Result, Space, Spin } from 'antd'
import { useEffect, useState, useSyncExternalStore } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import { currentRequestGeneration, watchRequestGeneration } from '../../api/client'
import AgentDock from '../../components/agent/AgentDock'
import MemberPanel from '../../components/collaboration/MemberPanel'
import ProjectManagementNotice from '../../components/project/ProjectManagementNotice'
import { readAgentPreferences, writeAgentPreferences } from '../../utils/agentPreferences'
import { startCollaboratorAccessPolling } from '../../utils/visiblePolling'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectStore } from '../../stores/project'
import { useAuthStore } from '../../stores/auth'

const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

/** 项目内容区：导航与面包屑由共享外壳渲染，保留项目加载、错误提示及原有智能体状态。 */
export default function ProjectLayout() {
  const pid = useProjectId()
  const location = useLocation()
  const actorId = useAuthStore((state) => state.user?.id)
  const sessionGeneration = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const { project, loading, error, load, scopeVersion } = useProjectStore()
  const { managingOther, isCollaborator, canUseAI, canViewAgent, authorizationVersion, canRead, canEdit, canManageCollaborators } = useProjectAccess(pid)
  const isMobile = useIsMobile()
  const [agentPrefs, setAgentPrefs] = useState(readAgentPreferences)
  const [membersOpenAt, setMembersOpenAt] = useState<number | null>(null)
  const membersOpen = membersOpenAt === scopeVersion
  const agentRequested = new URLSearchParams(location.search).get('agent') === 'open'
  const [dismissedAgentRequest, setDismissedAgentRequest] = useState<string | null>(null)
  const agentOpen = agentPrefs.open || (agentRequested && dismissedAgentRequest !== location.key)
  useEffect(() => writeAgentPreferences(agentPrefs), [agentPrefs])
  useEffect(() => { if (Number.isFinite(pid)) void load(pid) }, [pid, load])
  // 作者单独关闭作者模型授权等变化不会让本页请求失败；协作者在前台时定期复核权限快照。
  useEffect(() => startCollaboratorAccessPolling(isCollaborator, () => { void useProjectStore.getState().refresh() }, document, window),
    [pid, isCollaborator])

  if (!Number.isFinite(pid)) return <Result status="404" title="项目不存在" />
  if (error && (!project || project.id !== pid)) return <Result status="warning" title="无法打开项目" subTitle={error} extra={<Link to="/projects">返回作品书库</Link>} />
  if (!project || project.id !== pid) return <div style={{ padding: 48, textAlign: 'center' }}><Spin spinning={loading} /></div>

  if (!canRead) return <Result status="403" title="没有读取此作品的权限" />

  return (
    <div className="studio-project-workspace">
      <div className="studio-project-content">
        {(canViewAgent || canManageCollaborators) && <div className="studio-project-tools">
          <Space wrap>
            {canManageCollaborators && <Button icon={<TeamOutlined />} onClick={() => setMembersOpenAt(scopeVersion)}>协作者</Button>}
            {canViewAgent && <Button icon={<RobotOutlined />} aria-expanded={agentOpen} onClick={() => { setAgentPrefs((prev) => ({ ...prev, open: !agentOpen })); if (agentOpen) setDismissedAgentRequest(location.key) }}>项目智能体</Button>}
          </Space>
        </div>}
        {managingOther && !isCollaborator && project.owner && <ProjectManagementNotice owner={project.owner} />}
        {!canEdit && <Alert type="info" showIcon message={isCollaborator ? '当前为查看者，不能修改此作品' : '当前为只读内容管理，不能修改此作品'} style={{ marginBottom: 12 }} />}
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
        {!canUseAI && location.pathname.endsWith('/wizard') ? <Result status="info" title="当前角色不能使用创作向导" extra={<Link to={`/projects/${pid}/overview`}>返回项目概览</Link>} /> : <div key={`${pid}:${actorId ?? ''}:${sessionGeneration}`}><Outlet /></div>}
      </div>
      {/* 作者与可用 AI 的编辑者可写；查看者 / 管理员兼查看者只读本人会话（裁决 1、4）；纯管理员不挂载。
          按作品 + 协作授权轮次 + 能力代次键控：授权变化后整体重挂，不沿用旧的可写状态。 */}
      {canViewAgent && <AgentDock key={`${pid}:${authorizationVersion ?? 'owner'}:${scopeVersion}`} projectId={pid} open={agentOpen} mobile={isMobile} width={agentPrefs.width} onWidth={(width) => setAgentPrefs((prev) => ({ ...prev, width }))} onClose={() => { setAgentPrefs((prev) => ({ ...prev, open: false })); setDismissedAgentRequest(location.key) }} />}
      {canManageCollaborators && membersOpen && <Drawer title="作品协作者" open onClose={() => setMembersOpenAt(null)} width={isMobile ? '100%' : 760} destroyOnHidden>
        <MemberPanel scope="project" resourceId={pid} canManage />
      </Drawer>}
    </div>
  )
}
