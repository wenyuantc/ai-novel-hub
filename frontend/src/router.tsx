import type { ComponentType } from 'react'
import { createBrowserRouter, Navigate } from 'react-router-dom'
import AppLayout from './components/AppLayout'
import RequireAdmin from './components/RequireAdmin'
import RequireAuth from './components/RequireAuth'
import AdminUsersPage from './pages/AdminUsersPage'
import LoginPage from './pages/LoginPage'
import NotFoundPage from './pages/NotFoundPage'
import ProjectListPage from './pages/ProjectListPage'
import ChaptersPanel from './pages/project/ChaptersPanel'
import CharactersPanel from './pages/project/CharactersPanel'
import OutlinesPanel from './pages/project/OutlinesPanel'
import OverviewPanel from './pages/project/OverviewPanel'
import ProjectLayout from './pages/project/ProjectLayout'
import WizardPage from './pages/project/WizardPage'
import WorldPanel from './pages/project/WorldPanel'

/**
 * 路由级懒加载（React Router `lazy`）：主包需低于 chunkSizeWarningLimit（1500 kB），
 * 较重或较少访问的页面单独成 chunk（章节编辑器含 diff、组织 / 关系页、模型配置）。
 */
const lazyPage = (load: () => Promise<{ default: ComponentType }>) => async () => ({ Component: (await load()).default })

export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  {
    path: '/',
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Navigate to="/projects" replace /> },
      { path: 'projects', element: <ProjectListPage /> },
      { path: 'settings/ai', lazy: lazyPage(() => import('./pages/AIConfigPage')) },
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
          { path: 'outlines', element: <OutlinesPanel /> },
          { path: 'chapters', element: <ChaptersPanel /> },
          { path: 'chapters/:cid', lazy: lazyPage(() => import('./pages/project/ChapterEditorPage')) },
        ],
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
])
