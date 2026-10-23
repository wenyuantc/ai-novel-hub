import {
  ApartmentOutlined,
  BookOutlined,
  ClusterOutlined,
  DashboardOutlined,
  GlobalOutlined,
  OrderedListOutlined,
  RocketOutlined,
  TeamOutlined,
} from '@ant-design/icons'
import { Alert, Breadcrumb, Layout, Menu, Result, Spin, Tag } from 'antd'
import { useEffect } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import { PROJECT_STATUS } from '../../api/projects'
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
  { key: 'outlines', icon: <OrderedListOutlined />, label: '大纲' },
  { key: 'chapters', icon: <BookOutlined />, label: '章节' },
]

/** 项目工作区：左侧导航 + 右侧面板。 */
export default function ProjectLayout() {
  const pid = useProjectId()
  const location = useLocation()
  const { project, loading, error, load } = useProjectStore()

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

  return (
    <Layout style={{ minHeight: 'calc(100vh - 64px)' }}>
      <Sider width={200} theme="light" breakpoint="lg" collapsedWidth={56} style={{ borderRight: '1px solid #f0f0f0' }}>
        <Menu
          mode="inline"
          selectedKeys={[selected]}
          style={{ borderRight: 0, paddingTop: 8 }}
          items={NAV.map((n) => ({
            key: n.key,
            icon: n.icon,
            label: <Link to={`/projects/${pid}/${n.key}`}>{n.label}</Link>,
          }))}
        />
      </Sider>
      <Content style={{ padding: '16px 24px', minWidth: 0 }}>
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
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
        <Outlet />
      </Content>
    </Layout>
  )
}
