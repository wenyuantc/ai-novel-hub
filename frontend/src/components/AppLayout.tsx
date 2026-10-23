import { BookOutlined, LogoutOutlined, SettingOutlined, UserOutlined } from '@ant-design/icons'
import { Button, Dropdown, Layout, Menu, Space } from 'antd'
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuthStore } from '../stores/auth'

const { Header, Content } = Layout

/** 顶层布局：顶栏（导航 + 用户名 + 登出）。 */
export default function AppLayout() {
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)
  const location = useLocation()
  const navigate = useNavigate()

  const selected = location.pathname.startsWith('/settings') ? 'settings' : 'projects'

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ display: 'flex', alignItems: 'center', gap: 24, paddingInline: 24 }}>
        <Link to="/projects" style={{ color: '#fff', fontSize: 18, fontWeight: 600, whiteSpace: 'nowrap' }}>
          AI 小说平台
        </Link>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[selected]}
          style={{ flex: 1, minWidth: 0 }}
          items={[
            { key: 'projects', icon: <BookOutlined />, label: <Link to="/projects">我的项目</Link> },
            { key: 'settings', icon: <SettingOutlined />, label: <Link to="/settings/ai">模型配置</Link> },
          ]}
        />
        <Dropdown
          menu={{
            items: [{ key: 'logout', icon: <LogoutOutlined />, label: '退出登录' }],
            onClick: async ({ key }) => {
              if (key === 'logout') {
                await logout()
                navigate('/login', { replace: true })
              }
            },
          }}
        >
          <Button type="text" style={{ color: '#fff' }}>
            <Space>
              <UserOutlined />
              {user?.username}
            </Space>
          </Button>
        </Dropdown>
      </Header>
      <Content>
        <Outlet />
      </Content>
    </Layout>
  )
}
