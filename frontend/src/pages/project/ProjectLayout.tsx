import { RobotOutlined } from '@ant-design/icons'
import { Alert, Button, Result, Spin } from 'antd'
import { useEffect, useState } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import AgentDock from '../../components/agent/AgentDock'
import ProjectManagementNotice from '../../components/project/ProjectManagementNotice'
import { readAgentPreferences, writeAgentPreferences } from '../../utils/agentPreferences'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectStore } from '../../stores/project'

/** 项目内容区：导航与面包屑由共享外壳渲染，保留项目加载、错误提示及原有智能体状态。 */
export default function ProjectLayout() {
  const pid = useProjectId()
  const location = useLocation()
  const { project, loading, error, load } = useProjectStore()
  const { managingOther, canUseAI } = useProjectAccess(pid)
  const isMobile = useIsMobile()
  const [agentPrefs, setAgentPrefs] = useState(readAgentPreferences)
  const agentRequested = new URLSearchParams(location.search).get('agent') === 'open'
  const [dismissedAgentRequest, setDismissedAgentRequest] = useState<string | null>(null)
  const agentOpen = agentPrefs.open || (agentRequested && dismissedAgentRequest !== location.key)
  useEffect(() => writeAgentPreferences(agentPrefs), [agentPrefs])
  useEffect(() => { if (Number.isFinite(pid)) void load(pid) }, [pid, load])

  if (!Number.isFinite(pid)) return <Result status="404" title="项目不存在" />
  if (error && (!project || project.id !== pid)) return <Result status="warning" title="无法打开项目" subTitle={error} extra={<Link to="/projects">返回作品书库</Link>} />
  if (!project || project.id !== pid) return <div style={{ padding: 48, textAlign: 'center' }}><Spin spinning={loading} /></div>

  return (
    <div className="studio-project-workspace">
      <div className="studio-project-content">
        {canUseAI && <div className="studio-project-tools">
          <Button icon={<RobotOutlined />} aria-expanded={agentOpen} onClick={() => { setAgentPrefs((prev) => ({ ...prev, open: !agentOpen })); if (agentOpen) setDismissedAgentRequest(location.key) }}>项目智能体</Button>
        </div>}
        {managingOther && project.owner && <ProjectManagementNotice owner={project.owner} />}
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
        {managingOther && location.pathname.endsWith('/wizard') ? <Result status="info" title="当前为人工内容管理" extra={<Link to={`/projects/${pid}/overview`}>返回项目概览</Link>} /> : <Outlet />}
      </div>
      {canUseAI && <AgentDock projectId={pid} open={agentOpen} mobile={isMobile} width={agentPrefs.width} onWidth={(width) => setAgentPrefs((prev) => ({ ...prev, width }))} onClose={() => { setAgentPrefs((prev) => ({ ...prev, open: false })); setDismissedAgentRequest(location.key) }} />}
    </div>
  )
}
