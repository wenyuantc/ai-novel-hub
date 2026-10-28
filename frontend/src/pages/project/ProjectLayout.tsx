import {
  ApartmentOutlined,
  BookOutlined,
  BulbOutlined,
  ClusterOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  HistoryOutlined,
  GlobalOutlined,
  MenuOutlined,
  OrderedListOutlined,
  RocketOutlined,
  RobotOutlined,
  TeamOutlined,
  TrophyOutlined,
} from '@ant-design/icons'
import { Alert, Breadcrumb, Button, Drawer, Layout, Menu, Result, Spin, Tag, theme } from 'antd'
import { useEffect, useState } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import AgentDock from '../../components/agent/AgentDock'
import { readAgentPreferences, writeAgentPreferences } from '../../utils/agentPreferences'
import { PROJECT_STATUS } from '../../api/projects'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectStore } from '../../stores/project'

const { Sider, Content } = Layout

const NAV = [
  { key: 'overview', icon: <DashboardOutlined />, label: '概览' },
  { key: 'wizard', icon: <RocketOutlined />, label: '创作向导' },
  { key: 'world', icon: <GlobalOutlined />, label: '世界观' },
  { key: 'characters', icon: <TeamOutlined />, label: '角色' },
  { key: 'organizations', icon: <ClusterOutlined />, label: '组织' },
  { key: 'relationships', icon: <ApartmentOutlined />, label: '关系' },
  { key: 'careers', icon: <TrophyOutlined />, label: '职业' },
  { key: 'outlines', icon: <OrderedListOutlined />, label: '大纲' },
  { key: 'chapters', icon: <BookOutlined />, label: '章节' },
  { key: 'foreshadows', icon: <BulbOutlined />, label: '伏笔' },
  { key: 'knowledge', icon: <DatabaseOutlined />, label: '知识库' },
  { key: 'memories', icon: <HistoryOutlined />, label: '剧情记忆' },
]

/** 项目工作区：左侧导航 + 右侧面板；移动端不渲染侧栏，改为顶部「项目菜单」按钮打开左侧抽屉（选中后自动关闭）。 */
export default function ProjectLayout() {
  const pid = useProjectId()
  const location = useLocation()
  const { project, loading, error, load } = useProjectStore()
  const isMobile = useIsMobile()
  const [agentPrefs, setAgentPrefs] = useState(readAgentPreferences)
  const agentRequested = new URLSearchParams(location.search).get('agent') === 'open'
  const [dismissedAgentRequest, setDismissedAgentRequest] = useState<string | null>(null)
  const agentOpen = agentPrefs.open || (agentRequested && dismissedAgentRequest !== location.key)
  useEffect(() => writeAgentPreferences(agentPrefs), [agentPrefs])
  // 抽屉打开时记下当时的路径：导航（含浏览器后退）或切到桌面布局后自动视为关闭
  const [navOpenAt, setNavOpenAt] = useState<string | null>(null)
  const navOpen = isMobile && navOpenAt === location.pathname
  const setNavOpen = (open: boolean) => setNavOpenAt(open ? location.pathname : null)
  const { token } = theme.useToken()

  useEffect(() => {
    if (Number.isFinite(pid)) void load(pid)
  }, [pid, load])

  if (!Number.isFinite(pid)) return <Result status="404" title="项目不存在" />

  const segment = location.pathname.split('/')[3] ?? 'overview'
  const selected = NAV.some((n) => n.key === segment) ? segment : 'overview'

  if (error && !project) {
    return (
      <Result
        status="warning"
        title="无法打开项目"
        subTitle={error}
        extra={<Link to="/projects">返回项目列表</Link>}
      />
    )
  }
  if (!project || project.id !== pid) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Spin spinning={loading} />
      </div>
    )
  }

  const menu = (
    <Menu
      mode="inline"
      selectedKeys={[selected]}
      style={{ borderInlineEnd: 0, paddingTop: 8 }}
      onClick={() => setNavOpen(false)}
      items={NAV.map((n) => ({
        key: n.key,
        icon: n.icon,
        label: <Link to={`/projects/${pid}/${n.key}`}>{n.label}</Link>,
      }))}
    />
  )
  const current = NAV.find((n) => n.key === selected)

  return (
    <Layout style={{ minHeight: 'calc(100vh - 64px)' }}>
      {!isMobile && (
        <Sider
          width={200}
          theme="light"
          breakpoint="lg"
          collapsedWidth={56}
          style={{ borderRight: `1px solid ${token.colorBorderSecondary}` }}
        >
          {menu}
        </Sider>
      )}
      <Content style={{ padding: isMobile ? 12 : '16px 24px', minWidth: 0 }}>
        {isMobile && (
          <Button icon={<MenuOutlined />} style={{ marginBottom: 8 }} onClick={() => setNavOpen(true)}>
            项目菜单{current ? ` · ${current.label}` : ''}
          </Button>
        )}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8, flexWrap: 'wrap' }}>
        <Breadcrumb
          style={{ marginBottom: 12 }}
          items={[
            { title: <Link to="/projects">我的项目</Link> },
            {
              title: (
                <span>
                  {project.title} <Tag color={PROJECT_STATUS[project.status].color}>{PROJECT_STATUS[project.status].label}</Tag>
                </span>
              ),
            },
          ]}
        />
        <Button icon={<RobotOutlined />} aria-expanded={agentOpen} onClick={() => { setAgentPrefs((prev) => ({ ...prev, open: !agentOpen })); if (agentOpen) setDismissedAgentRequest(location.key) }}>项目智能体</Button>
        </div>
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
        <Outlet />
      </Content>
      <AgentDock projectId={pid} open={agentOpen} mobile={isMobile} width={agentPrefs.width} onWidth={(width) => setAgentPrefs((prev) => ({ ...prev, width }))} onClose={() => { setAgentPrefs((prev) => ({ ...prev, open: false })); setDismissedAgentRequest(location.key) }} />
      {isMobile && (
        <Drawer
          title={project.title}
          placement="left"
          width={240}
          open={navOpen}
          onClose={() => setNavOpen(false)}
          styles={{ body: { padding: 0 } }}
        >
          {menu}
        </Drawer>
      )}
    </Layout>
  )
}
