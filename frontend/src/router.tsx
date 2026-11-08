import type { ComponentType } from 'react'
import { createBrowserRouter, Navigate, Outlet } from 'react-router-dom'
import AppLayout from './components/AppLayout'
import RequirePermission from './components/RequirePermission'
import RequireAuth from './components/RequireAuth'
import LoginPage from './pages/LoginPage'
import NotFoundPage from './pages/NotFoundPage'
import ProjectListPage from './pages/ProjectListPage'
import ChaptersPanel from './pages/project/ChaptersPanel'
import CharactersPanel from './pages/project/CharactersPanel'
import OverviewPanel from './pages/project/OverviewPanel'
import ProjectLayout from './pages/project/ProjectLayout'
import WizardPage from './pages/project/WizardPage'
import WorldPanel from './pages/project/WorldPanel'

/**
 * 路由级懒加载（React Router `lazy`）：主包需低于 chunkSizeWarningLimit（1500 kB），
 * 较重或较少访问的页面单独成 chunk（章节编辑器含 diff、阅读器、大纲 / 组织 / 关系 / 职业 / 伏笔 / 知识库 / 剧情记忆页、模型配置、提示词模板、写作风格库、Skill、MCP 插件、灵感模式、拆书导入）。
 */
const lazyPage = (load: () => Promise<{ default: ComponentType }>) => async () => ({ Component: (await load()).default })

export const router = createBrowserRouter([
  { path: '/', lazy: lazyPage(() => import('./pages/HomePage')) },
  { path: '/developers', lazy: lazyPage(() => import('./pages/DeveloperDocsPage')) },
  { path: '/login', element: <LoginPage /> },
  // 阅读器：全屏，不在 AppLayout / ProjectLayout 内；不带 :cid 时按阅读进度定位
  {
    path: '/projects/:pid/read',
    element: (
      <RequireAuth>
        <Outlet />
      </RequireAuth>
    ),
    children: [
      { index: true, lazy: lazyPage(() => import('./pages/ReaderPage')) },
      { path: ':cid', lazy: lazyPage(() => import('./pages/ReaderPage')) },
    ],
  },
  {
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [
      { path: 'projects', element: <ProjectListPage /> },
      { path: 'series', lazy: lazyPage(() => import('./pages/SeriesPage')) },
      { path: 'series/:sid', lazy: lazyPage(() => import('./pages/SeriesPage')) },
      { path: 'settings/ai', lazy: lazyPage(() => import('./pages/AIConfigPage')) },
      { path: 'settings/api-keys', lazy: lazyPage(() => import('./pages/ApiKeysPage')) },
      { path: 'prompt-templates', lazy: lazyPage(() => import('./pages/PromptTemplatesPage')) },
      { path: 'prompt-components', lazy: lazyPage(() => import('./pages/PromptComponentsPage')) },
      { path: 'workshop', lazy: lazyPage(() => import('./pages/WorkshopPage')) },
      { path: 'styles', lazy: lazyPage(() => import('./pages/StylesPage')) },
      { path: 'skills', lazy: lazyPage(() => import('./pages/SkillsPage')) },
      { path: 'mcp-plugins', lazy: lazyPage(() => import('./pages/McpPluginsPage')) },
      { path: 'inspiration', lazy: lazyPage(() => import('./pages/InspirationListPage')) },
      { path: 'inspiration/:sid', lazy: lazyPage(() => import('./pages/InspirationSessionPage')) },
      { path: 'book-import', lazy: lazyPage(() => import('./pages/BookImportPage')) },
      {
        path: 'admin/backups',
        element: <RequirePermission permission="backups.read"><Outlet /></RequirePermission>,
        children: [{ index: true, lazy: lazyPage(() => import('./pages/AdminBackupsPage')) }],
      },
      {
        path: 'admin/system-settings',
        element: <RequirePermission permission="settings.read"><Outlet /></RequirePermission>,
        children: [{ index: true, lazy: lazyPage(() => import('./pages/AdminSystemSettingsPage')) }],
      },
      {
        path: 'admin/dashboard',
        element: <RequirePermission permission="dashboard.read"><Outlet /></RequirePermission>,
        children: [{ index: true, lazy: lazyPage(() => import('./pages/AdminDashboardPage')) }],
      },
      {
        path: 'admin/tasks',
        element: <RequirePermission permission="tasks.read"><Outlet /></RequirePermission>,
        children: [{ index: true, lazy: lazyPage(() => import('./pages/AdminTasksPage')) }],
      },
      {
        path: 'admin/recycle-bin',
        element: <RequirePermission permission="recycle_bin.read"><Outlet /></RequirePermission>,
        children: [{ index: true, lazy: lazyPage(() => import('./pages/AdminRecycleBinPage')) }],
      },
      {
        path: 'admin/audit-logs',
        element: <RequirePermission permission="audit.read"><Outlet /></RequirePermission>,
        children: [{ index: true, lazy: lazyPage(() => import('./pages/AdminAuditLogsPage')) }],
      },
      {
        path: 'admin/projects',
        element: <RequirePermission permission="projects.read"><Outlet /></RequirePermission>,
        children: [{ index: true, lazy: lazyPage(() => import('./pages/AdminProjectsPage')) }],
      },
      {
        path: 'admin/users',
        element: <RequirePermission permission="users.read"><Outlet /></RequirePermission>,
        children: [
          { index: true, lazy: lazyPage(() => import('./pages/AdminUsersPage')) },
          { path: ':userId', lazy: lazyPage(() => import('./pages/AdminUserDetailsPage')) },
        ],
      },
      { path: 'admin/roles', element: <RequirePermission superOnly><Outlet /></RequirePermission>, children: [{ index: true, lazy: lazyPage(() => import('./pages/AdminRolesPage')) }] },
      {
        path: 'projects/:pid',
        element: <ProjectLayout />,
        children: [
          { index: true, element: <Navigate to="overview" replace /> },
          { path: 'overview', element: <OverviewPanel /> },
          { path: 'wizard', element: <WizardPage /> },
          { path: 'world', element: <WorldPanel /> },
          { path: 'characters', element: <CharactersPanel /> },
          { path: 'organizations', lazy: lazyPage(() => import('./pages/project/OrganizationsPanel')) },
          { path: 'relationships', lazy: lazyPage(() => import('./pages/project/RelationshipsPanel')) },
          { path: 'careers', lazy: lazyPage(() => import('./pages/project/CareersPanel')) },
          { path: 'setting-fields', lazy: lazyPage(() => import('./pages/project/SettingFieldsPanel')) },
          { path: 'setting-categories', lazy: lazyPage(() => import('./pages/project/SettingCategoriesPanel')) },
          { path: 'foreshadows', lazy: lazyPage(() => import('./pages/project/ForeshadowsPanel')) },
          { path: 'knowledge', lazy: lazyPage(() => import('./pages/project/KnowledgePanel')) },
          { path: 'notes', lazy: lazyPage(() => import('./pages/project/NotesPanel')) },
          { path: 'mentions', lazy: lazyPage(() => import('./pages/project/MentionsPanel')) },
          { path: 'memories', lazy: lazyPage(() => import('./pages/project/MemoriesPanel')) },
          { path: 'outlines', lazy: lazyPage(() => import('./pages/project/OutlinesPanel')) },
          { path: 'planning', lazy: lazyPage(() => import('./pages/project/PlanningPanel')) },
          { path: 'chapters', element: <ChaptersPanel /> },
          { path: 'chapters/:cid', lazy: lazyPage(() => import('./pages/project/ChapterEditorPage')) },
        ],
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
])
