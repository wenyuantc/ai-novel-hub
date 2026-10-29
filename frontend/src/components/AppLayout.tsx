import {
  ApiOutlined,
  BgColorsOutlined,
  BulbOutlined,
  BookOutlined,
  FileTextOutlined,
  KeyOutlined,
  LogoutOutlined,
  MenuOutlined,
  SettingOutlined,
  ShopOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Button, Drawer, Dropdown, Layout, Menu, Space, theme, Typography } from 'antd'
import type { MenuProps } from 'antd'
import { useState } from 'react'
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useIsMobile } from '../hooks/useIsMobile'
import { useAuthStore } from '../stores/auth'
import ThemeSwitch from '../theme/ThemeSwitch'
import ChangePasswordModal from './ChangePasswordModal'
import TaskPanel from './TaskPanel'

const { Header, Content } = Layout

/**
 * 顶层布局：顶栏（导航：我的项目 / 模型配置 / 提示词模板 / 写作风格 / Skill / MCP 插件（部署关闭 MCP 时隐藏）；主题切换；用户菜单：提示词模板 / 写作风格库 / 用户管理（仅管理员）/ 修改密码 / 登出）
 * + 右下角后台任务面板。移动端顶栏只留 logo、汉堡按钮与用户菜单，导航与主题切换收进右侧抽屉。
 */
export default function AppLayout() {
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)
  const mcpEnabled = useAuthStore((s) => s.mcpEnabled)
  const location = useLocation()
  const navigate = useNavigate()
  const [passwordOpen, setPasswordOpen] = useState(false)
  // 抽屉打开时记下当时的路径：导航（含浏览器后退）或切到桌面布局后自动视为关闭
  const [navOpenAt, setNavOpenAt] = useState<string | null>(null)
  const isMobile = useIsMobile()
  const navOpen = isMobile && navOpenAt === location.pathname
  const setNavOpen = (open: boolean) => setNavOpenAt(open ? location.pathname : null)
  const { token } = theme.useToken()
  const onBar = token.colorTextLightSolid

  const userMenuItems: MenuProps['items'] = [
    { key: 'prompt-templates', icon: <FileTextOutlined />, label: '提示词模板' },
    { key: 'workshop', icon: <ShopOutlined />, label: '提示词工坊' },
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
      : path.startsWith('/workshop')
        ? ['workshop']
      : path.startsWith('/styles')
        ? ['styles']
        : path.startsWith('/skills')
          ? ['skills']
          : path.startsWith('/mcp-plugins')
            ? ['mcp']
            : path.startsWith('/admin')
              ? []
              : ['projects']

  const navItems = [
    { key: 'projects', icon: <BookOutlined />, label: <Link to="/projects">我的项目</Link> },
    { key: 'settings', icon: <SettingOutlined />, label: <Link to="/settings/ai">模型配置</Link> },
    { key: 'prompts', icon: <FileTextOutlined />, label: <Link to="/prompt-templates">提示词模板</Link> },
    { key: 'styles', icon: <BgColorsOutlined />, label: <Link to="/styles">写作风格</Link> },
    { key: 'workshop', icon: <ShopOutlined />, label: <Link to="/workshop">提示词工坊</Link> },
    { key: 'skills', icon: <BulbOutlined />, label: <Link to="/skills">Skill</Link> },
    // 部署关闭 MCP（/auth/config mcp_enabled = false）时隐藏入口
    ...(mcpEnabled ? [{ key: 'mcp', icon: <ApiOutlined />, label: <Link to="/mcp-plugins">MCP 插件</Link> }] : []),
  ]

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header
        style={{ display: 'flex', alignItems: 'center', gap: isMobile ? 8 : 24, paddingInline: isMobile ? 12 : 24 }}
      >
        <Link to="/projects" style={{ color: onBar, fontSize: isMobile ? 16 : 18, fontWeight: 600, whiteSpace: 'nowrap' }}>
          AI 小说平台
        </Link>
        {isMobile ? (
          <div style={{ flex: 1 }} />
        ) : (
          <>
            <Menu theme="dark" mode="horizontal" selectedKeys={selected} style={{ flex: 1, minWidth: 0 }} items={navItems} />
            <ThemeSwitch compact onDarkBar />
          </>
        )}
        <Dropdown
          menu={{
            items: userMenuItems,
            onClick: async ({ key }) => {
              if (key === 'admin-users') navigate('/admin/users')
              else if (key === 'prompt-templates') navigate('/prompt-templates')
              else if (key === 'workshop') navigate('/workshop')
              else if (key === 'styles') navigate('/styles')
              else if (key === 'change-password') setPasswordOpen(true)
              else if (key === 'logout') {
                await logout()
                navigate('/login', { replace: true })
              }
            },
          }}
        >
          <Button type="text" style={{ color: onBar, paddingInline: isMobile ? 6 : undefined }}>
            <Space>
              <UserOutlined />
              {!isMobile && user?.username}
            </Space>
          </Button>
        </Dropdown>
        {isMobile && (
          <Button type="text" aria-label="打开菜单" icon={<MenuOutlined />} style={{ color: onBar }} onClick={() => setNavOpen(true)} />
        )}
      </Header>
      <Content style={{ minWidth: 0 }}>
        <Outlet />
      </Content>
      {isMobile && (
        <Drawer title={user?.username ?? '菜单'} placement="right" width={260} open={navOpen} onClose={() => setNavOpen(false)}>
          <Menu
            mode="inline"
            selectedKeys={selected}
            style={{ borderInlineEnd: 0 }}
            items={navItems}
            onClick={() => setNavOpen(false)}
          />
          <div style={{ marginTop: 16 }}>
            <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
              主题
            </Typography.Text>
            <ThemeSwitch />
          </div>
        </Drawer>
      )}
      <ChangePasswordModal open={passwordOpen} onClose={() => setPasswordOpen(false)} />
      <TaskPanel />
    </Layout>
  )
}
