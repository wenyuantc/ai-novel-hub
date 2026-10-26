import {
  BgColorsOutlined,
  BookOutlined,
  FileTextOutlined,
  KeyOutlined,
  LogoutOutlined,
  SettingOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Button, Dropdown, Layout, Menu, Space } from 'antd'
import type { MenuProps } from 'antd'
import { useState } from 'react'
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuthStore } from '../stores/auth'
import ChangePasswordModal from './ChangePasswordModal'
import TaskPanel from './TaskPanel'

const { Header, Content } = Layout

/** 顶层布局：顶栏（导航：我的项目 / 模型配置 / 提示词模板 / 写作风格；用户菜单：提示词模板 / 写作风格库 / 用户管理（仅管理员）/ 修改密码 / 登出）+ 右下角后台任务面板。 */
export default function AppLayout() {
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)
  const location = useLocation()
  const navigate = useNavigate()
  const [passwordOpen, setPasswordOpen] = useState(false)

  const userMenuItems: MenuProps['items'] = [
    { key: 'prompt-templates', icon: <FileTextOutlined />, label: '提示词模板' },
    { key: 'styles', icon: <BgColorsOutlined />, label: '写作风格库' },
    { type: 'divider' as const },
    ...(user?.is_admin ? [{ key: 'admin-users', icon: <TeamOutlined />, label: '用户管理' }] : []),
    { key: 'change-password', icon: <KeyOutlined />, label: '修改密码' },
    { type: 'divider' as const },
    { key: 'logout', icon: <LogoutOutlined />, label: '退出登录' },
  ]

  const path = location.pathname
  const selected = path.startsWith('/settings')
    ? ['settings']
    : path.startsWith('/prompt-templates')
      ? ['prompts']
      : path.startsWith('/styles')
        ? ['styles']
        : path.startsWith('/admin')
          ? []
          : ['projects']

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ display: 'flex', alignItems: 'center', gap: 24, paddingInline: 24 }}>
        <Link to="/projects" style={{ color: '#fff', fontSize: 18, fontWeight: 600, whiteSpace: 'nowrap' }}>
          AI 小说平台
        </Link>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={selected}
          style={{ flex: 1, minWidth: 0 }}
          items={[
            { key: 'projects', icon: <BookOutlined />, label: <Link to="/projects">我的项目</Link> },
            { key: 'settings', icon: <SettingOutlined />, label: <Link to="/settings/ai">模型配置</Link> },
            { key: 'prompts', icon: <FileTextOutlined />, label: <Link to="/prompt-templates">提示词模板</Link> },
            { key: 'styles', icon: <BgColorsOutlined />, label: <Link to="/styles">写作风格</Link> },
          ]}
        />
        <Dropdown
          menu={{
            items: userMenuItems,
            onClick: async ({ key }) => {
              if (key === 'admin-users') navigate('/admin/users')
              else if (key === 'prompt-templates') navigate('/prompt-templates')
              else if (key === 'styles') navigate('/styles')
              else if (key === 'change-password') setPasswordOpen(true)
              else if (key === 'logout') {
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
      <ChangePasswordModal open={passwordOpen} onClose={() => setPasswordOpen(false)} />
      <TaskPanel />
    </Layout>
  )
}
