import { AppstoreOutlined, ArrowLeftOutlined, AuditOutlined, BookOutlined, CloudDownloadOutlined, DashboardOutlined, DeleteOutlined, KeyOutlined, LineChartOutlined, LogoutOutlined, MenuOutlined, PlusOutlined, RightOutlined, SettingOutlined, ShopOutlined, TeamOutlined, UnorderedListOutlined } from '@ant-design/icons'
import { Button, Drawer, Dropdown } from 'antd'
import type { MenuProps } from 'antd'
import { useState, useSyncExternalStore } from 'react'
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { currentRequestGeneration, watchRequestGeneration } from '../api/client'
import { useIsMobile } from '../hooks/useIsMobile'
import { useAuthStore } from '../stores/auth'
import { useProjectStore } from '../stores/project'
import { useTasksStore } from '../stores/tasks'
import ThemeSwitch from '../theme/ThemeSwitch'
import { projectAccess, showsTaskPanel } from '../utils/projectAccess'
import { can, isSuperAdmin, firstAdminPath } from '../utils/permissions'
import NotificationInbox from './notifications/NotificationInbox'
import ChangePasswordModal from './ChangePasswordModal'
import { navigationItem, studioNavigation } from './StudioNavigation'
import { StudioBrand } from './StudioUI'
import TaskPanel from './TaskPanel'
import './AppLayout.css'

const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

/** 纸上书房外壳：216px 分组导航、56px 顶栏，真实账户与后台任务，移动端复用同一份导航。 */
export default function AppLayout() {
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)
  const mcpEnabled = useAuthStore((s) => s.mcpEnabled)
  const enabledPlugins = useAuthStore((s) => s.enabledPlugins)
  const cachedProject = useProjectStore((s) => s.project)
  const scopeVersion = useProjectStore((s) => s.scopeVersion)
  const activeCount = useTasksStore((s) => s.active.length)
  const tasksLoaded = useTasksStore((s) => s.loaded)
  const location = useLocation()
  const navigate = useNavigate()
  const isMobile = useIsMobile()
  const [passwordOpen, setPasswordOpen] = useState(false)
  const [tasksOpenAt, setTasksOpenAt] = useState<string | null>(null)
  const sessionGeneration = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const [navOpenAt, setNavOpenAt] = useState<string | null>(null)
  const navOpen = isMobile && navOpenAt === location.key
  const match = /^\/projects\/(\d+)(?:\/|$)/.exec(location.pathname)
  const projectId = match ? Number(match[1]) : null
  const project = cachedProject?.id === projectId ? cachedProject : null
  const access = projectAccess(project, user, projectId ?? NaN)
  const { managingOther, isCollaborator, canUseAI } = access
  // 协作者的共享书在个人书架；只有纯管理员代管才返回作品管理。
  const administeringOther = managingOther && !isCollaborator
  const showTaskPanel = showsTaskPanel(projectId, access)
  const taskScope = `${sessionGeneration}:${user?.id ?? ''}:${projectId ?? 'global'}:${projectId === null ? '' : scopeVersion}`
  const tasksOpen = tasksOpenAt === taskScope
  const groups = studioNavigation(projectId, mcpEnabled, canUseAI, access.canRead && (access.role === 'owner' || isCollaborator), enabledPlugins)
  const current = navigationItem(groups, location.pathname)
  const adminSection = location.pathname.startsWith('/admin/')
  const pageName = location.pathname === '/admin/plugins' ? '功能插件' : location.pathname === '/settings/security' ? '账号安全' : location.pathname === '/admin/publication-terms' ? '全站发布词表' : location.pathname.startsWith('/admin/ai-usage') ? '全站 AI 用量与费用' : location.pathname.startsWith('/admin/backups') ? '整库备份' : location.pathname.startsWith('/admin/roles') ? '角色与权限' : location.pathname.startsWith('/admin/system-settings') ? '系统设置' : location.pathname.startsWith('/admin/tasks') ? '全站任务监控' : location.pathname.startsWith('/admin/dashboard') ? '管理员首页' : location.pathname.startsWith('/admin/recycle-bin') ? '作品回收站' : location.pathname.startsWith('/admin/audit-logs') ? '操作日志' : location.pathname.startsWith('/admin/projects') ? '作品管理' : location.pathname.startsWith('/admin/users') ? '用户管理' : current?.label ?? '创作空间'
  const initial = Array.from(user?.username ?? '').at(0) ?? '我'

  const accountMenu: MenuProps = {
    items: [
      { key: 'workshop', icon: <ShopOutlined />, label: '提示词工坊' },
      ...(can(user, 'dashboard.read') ? [{ key: 'admin-dashboard', icon: <DashboardOutlined />, label: '管理员首页' }] : []),
      ...(can(user, 'tasks.read') ? [{ key: 'admin-tasks', icon: <UnorderedListOutlined />, label: '全站任务监控' }] : []),
      ...(can(user, 'ai_usage.read') ? [{ key: 'admin-ai-usage', icon: <LineChartOutlined aria-hidden />, label: '全站 AI 用量与费用' }] : []),
      ...(can(user, 'users.read') ? [{ key: 'admin', icon: <TeamOutlined />, label: '用户管理' }] : []),
      ...(can(user, 'projects.read') ? [{ key: 'admin-projects', icon: <BookOutlined />, label: '作品管理' }] : []),
      ...(can(user, 'recycle_bin.read') ? [{ key: 'admin-recycle-bin', icon: <DeleteOutlined />, label: '作品回收站' }] : []),
      ...(can(user, 'audit.read') ? [{ key: 'admin-audit-logs', icon: <AuditOutlined />, label: '操作日志' }] : []),
      ...(can(user, 'backups.read') ? [{ key: 'admin-backups', icon: <CloudDownloadOutlined />, label: '整库备份' }] : []),
      ...(can(user, 'plugins.read') ? [{ key: 'admin-plugins', icon: <AppstoreOutlined />, label: '功能插件' }] : []),
      ...(can(user, 'settings.read') ? [{ key: 'admin-system-settings', icon: <SettingOutlined />, label: '系统设置' }] : []),
      ...(isSuperAdmin(user) ? [{ key: 'admin-roles', icon: <TeamOutlined />, label: '角色与权限' }] : []),
      ...(can(user, 'publication_terms.read') ? [{ key: 'admin-publication-terms', icon: <BookOutlined />, label: '全站发布词表' }] : []),
      { type: 'divider' },
      { key: 'publication-terms', icon: <BookOutlined />, label: '个人发布词表' },
      { key: 'security', icon: <KeyOutlined />, label: '账号安全' },
      ...(user?.has_local_password === false ? [] : [{ key: 'password', icon: <KeyOutlined />, label: '修改密码' }]),
      { key: 'logout', icon: <LogoutOutlined />, label: '退出登录' },
    ],
    onClick: async ({ key }) => {
      setNavOpenAt(null)
      if (key === 'security') navigate('/settings/security')
      else if (key === 'admin-plugins') navigate('/admin/plugins')
      else if (key === 'admin-dashboard') navigate('/admin/dashboard')
      else if (key === 'admin-tasks') navigate('/admin/tasks')
      else if (key === 'admin-ai-usage') navigate('/admin/ai-usage')
      else if (key === 'admin') navigate('/admin/users')
      else if (key === 'admin-projects') navigate('/admin/projects')
      else if (key === 'admin-recycle-bin') navigate('/admin/recycle-bin')
      else if (key === 'admin-audit-logs') navigate('/admin/audit-logs')
      else if (key === 'admin-backups') navigate('/admin/backups')
      else if (key === 'admin-system-settings') navigate('/admin/system-settings')
      else if (key === 'admin-roles') navigate('/admin/roles')
      else if (key === 'workshop') navigate('/workshop')
      else if (key === 'publication-terms') navigate('/settings/publication-terms')
      else if (key === 'admin-publication-terms') navigate('/admin/publication-terms')
      else if (key === 'password') setPasswordOpen(true)
      else if (key === 'logout') {
        await logout()
        if (useAuthStore.getState().status === 'anonymous') navigate('/login', { replace: true })
      }
    },
  }
  const account = (compact = false) => (
    <Dropdown menu={accountMenu} trigger={['click']}>
      <button type="button" className="studio-account" aria-label={`账户设置：${user?.username ?? '当前账户'}`}>
        <span className="studio-avatar">{initial}</span>
        {!compact && <span className="studio-account-name">{user?.username}</span>}
      </button>
    </Dropdown>
  )

  const sidebar = (
    <>
      <StudioBrand onClick={() => setNavOpenAt(null)} />
      {projectId !== null ? (
        <div className="studio-project-switch">
          <Link to={administeringOther ? '/admin/projects' : '/projects'} onClick={() => setNavOpenAt(null)}><ArrowLeftOutlined /> {administeringOther ? '作品管理' : '全部作品'}</Link>
          <Link to={`/projects/${projectId}/overview`} onClick={() => setNavOpenAt(null)} title={project?.title}>{project?.title ?? '正在载入作品…'}</Link>
        </div>
      ) : <Button type="primary" className="studio-new-project" icon={<PlusOutlined />} onClick={() => { setNavOpenAt(null); navigate('/projects?create=1') }}>新建作品</Button>}
      <nav className="studio-navigation" aria-label={projectId !== null ? '作品功能目录' : '全局功能导航'}>
        {groups.map((group) => (
          <div className="studio-nav-group" key={group.title}>
            <p>{group.title}</p>
            {group.items.map((item) => (
              <Link key={item.to} to={item.to} aria-current={current?.to === item.to ? 'page' : undefined} onClick={() => setNavOpenAt(null)}>
                {item.icon}<span>{item.label}</span>
              </Link>
            ))}
          </div>
        ))}
      </nav>
      <div className="studio-sidebar-footer">
        {projectId !== null && !administeringOther && <Link to="/settings/ai" className="studio-nav-action" onClick={() => setNavOpenAt(null)}><SettingOutlined />模型与创作资源</Link>}
        {showTaskPanel && <button type="button" className="studio-nav-action" onClick={() => { setNavOpenAt(null); setTasksOpenAt(taskScope) }}><UnorderedListOutlined />后台任务{tasksLoaded && <span className="studio-task-count">{activeCount}</span>}</button>}
        {can(user, 'dashboard.read') && projectId === null && <Link className="studio-nav-action" to="/admin/dashboard" aria-current={location.pathname.startsWith('/admin/dashboard') ? 'page' : undefined} onClick={() => setNavOpenAt(null)}><DashboardOutlined />管理员首页</Link>}
        {can(user, 'tasks.read') && projectId === null && <Link className="studio-nav-action" to="/admin/tasks" aria-current={location.pathname.startsWith('/admin/tasks') ? 'page' : undefined} onClick={() => setNavOpenAt(null)}><UnorderedListOutlined />全站任务监控</Link>}
        {can(user, 'ai_usage.read') && projectId === null && <Link className="studio-nav-action" to="/admin/ai-usage" aria-current={location.pathname.startsWith('/admin/ai-usage') ? 'page' : undefined} onClick={() => setNavOpenAt(null)}><LineChartOutlined aria-hidden />全站 AI 用量与费用</Link>}
        {can(user, 'users.read') && projectId === null && <Link className="studio-nav-action" to="/admin/users" aria-current={location.pathname.startsWith('/admin/users') ? 'page' : undefined} onClick={() => setNavOpenAt(null)}><TeamOutlined />用户管理</Link>}
        {can(user, 'projects.read') && projectId === null && <Link className="studio-nav-action" to="/admin/projects" aria-current={location.pathname.startsWith('/admin/projects') ? 'page' : undefined} onClick={() => setNavOpenAt(null)}><BookOutlined />作品管理</Link>}
        {can(user, 'recycle_bin.read') && projectId === null && <Link className="studio-nav-action" to="/admin/recycle-bin" aria-current={location.pathname.startsWith('/admin/recycle-bin') ? 'page' : undefined} onClick={() => setNavOpenAt(null)}><DeleteOutlined />作品回收站</Link>}
        {can(user, 'audit.read') && projectId === null && <Link className="studio-nav-action" to="/admin/audit-logs" aria-current={location.pathname.startsWith('/admin/audit-logs') ? 'page' : undefined} onClick={() => setNavOpenAt(null)}><AuditOutlined />操作日志</Link>}
        {can(user, 'backups.read') && projectId === null && <Link className="studio-nav-action" to="/admin/backups" aria-current={location.pathname.startsWith('/admin/backups') ? 'page' : undefined} onClick={() => setNavOpenAt(null)}><CloudDownloadOutlined />整库备份</Link>}
        {can(user, 'publication_terms.read') && projectId === null && <Link className="studio-nav-action" to="/admin/publication-terms" aria-current={location.pathname === '/admin/publication-terms' ? 'page' : undefined} onClick={() => setNavOpenAt(null)}><BookOutlined />全站发布词表</Link>}
        {can(user, 'plugins.read') && projectId === null && <Link className="studio-nav-action" to="/admin/plugins" aria-current={location.pathname === '/admin/plugins' ? 'page' : undefined} onClick={() => setNavOpenAt(null)}><AppstoreOutlined />功能插件</Link>}
        {can(user, 'settings.read') && projectId === null && <Link className="studio-nav-action" to="/admin/system-settings" aria-current={location.pathname.startsWith('/admin/system-settings') ? 'page' : undefined} onClick={() => setNavOpenAt(null)}><SettingOutlined />系统设置</Link>}
        {isSuperAdmin(user) && projectId === null && <Link className="studio-nav-action" to="/admin/roles" onClick={() => setNavOpenAt(null)}><TeamOutlined />角色与权限</Link>}
        <div className="studio-sidebar-account">{account()}<ThemeSwitch compact /></div>
      </div>
    </>
  )

  return (
    <div className="studio-shell">
      {!isMobile && <aside className="studio-sidebar">{sidebar}</aside>}
      <div className="studio-workspace">
        <header className="studio-topbar">
          {isMobile && <Button type="text" aria-label="打开菜单" icon={<MenuOutlined />} onClick={() => setNavOpenAt(location.key)} />}
          <nav className="studio-breadcrumb" aria-label="面包屑导航">
            <Link to={administeringOther ? '/admin/projects' : adminSection ? firstAdminPath(user) : '/projects'}>{administeringOther ? '作品管理' : adminSection ? '管理后台' : '我的作品'}</Link><RightOutlined />
            {project && <><Link to={`/projects/${project.id}/overview`} title={project.title}>{project.title}</Link><RightOutlined /></>}
            <span aria-current="page">{pageName}</span>
          </nav>
          <div className="studio-topbar-account">
            <span className="studio-tagline">把想象，写成一页页故事。</span>
            {user && <NotificationInbox key={`${sessionGeneration}:${user.id}:${user.created_at}:${user.authorization_revision}:${scopeVersion}`}
              actorId={user.id} createdAt={user.created_at} />}
            {account(true)}
          </div>
        </header>
        <div className="studio-content"><Outlet /></div>
      </div>
      {isMobile && <Drawer placement="left" width={280} open={navOpen} onClose={() => setNavOpenAt(null)} className="studio-mobile-drawer" styles={{ body: { padding: 0 } }} aria-label="创作导航"><aside className="studio-sidebar studio-sidebar-mobile">{sidebar}</aside></Drawer>}
      <ChangePasswordModal open={passwordOpen} onClose={() => setPasswordOpen(false)} />
      {showTaskPanel && <TaskPanel key={taskScope} open={tasksOpen} onOpenChange={(open) => setTasksOpenAt(open ? taskScope : null)} hideTrigger />}
    </div>
  )
}
