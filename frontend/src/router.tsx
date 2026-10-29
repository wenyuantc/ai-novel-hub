import type { ComponentType } from 'react'
import { createBrowserRouter, Navigate, Outlet } from 'react-router-dom'
import AppLayout from './components/AppLayout'
import RequireAdmin from './components/RequireAdmin'
import RequireAuth from './components/RequireAuth'
import AdminUsersPage from './pages/AdminUsersPage'
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
      { path: 'settings/ai', lazy: lazyPage(() => import('./pages/AIConfigPage')) },
      { path: 'prompt-templates', lazy: lazyPage(() => import('./pages/PromptTemplatesPage')) },
      { path: 'workshop', lazy: lazyPage(() => import('./pages/WorkshopPage')) },
      { path: 'styles', lazy: lazyPage(() => import('./pages/StylesPage')) },
      { path: 'skills', lazy: lazyPage(() => import('./pages/SkillsPage')) },
      { path: 'mcp-plugins', lazy: lazyPage(() => import('./pages/McpPluginsPage')) },
      { path: 'inspiration', lazy: lazyPage(() => import('./pages/InspirationListPage')) },
      { path: 'inspiration/:sid', lazy: lazyPage(() => import('./pages/InspirationSessionPage')) },
      { path: 'book-import', lazy: lazyPage(() => import('./pages/BookImportPage')) },
      {
        path: 'admin/users',
        element: (
          <RequireAdmin>
            <AdminUsersPage />
          </RequireAdmin>
        ),
      },
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
          { path: 'foreshadows', lazy: lazyPage(() => import('./pages/project/ForeshadowsPanel')) },
          { path: 'knowledge', lazy: lazyPage(() => import('./pages/project/KnowledgePanel')) },
          { path: 'memories', lazy: lazyPage(() => import('./pages/project/MemoriesPanel')) },
          { path: 'outlines', lazy: lazyPage(() => import('./pages/project/OutlinesPanel')) },
          { path: 'chapters', element: <ChaptersPanel /> },
          { path: 'chapters/:cid', lazy: lazyPage(() => import('./pages/project/ChapterEditorPage')) },
        ],
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
])
