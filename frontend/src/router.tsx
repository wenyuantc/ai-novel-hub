import { createBrowserRouter, Navigate } from 'react-router-dom'
import AppLayout from './components/AppLayout'
import RequireAdmin from './components/RequireAdmin'
import RequireAuth from './components/RequireAuth'
import AdminUsersPage from './pages/AdminUsersPage'
import AIConfigPage from './pages/AIConfigPage'
import LoginPage from './pages/LoginPage'
import NotFoundPage from './pages/NotFoundPage'
import ProjectListPage from './pages/ProjectListPage'
import ChapterEditorPage from './pages/project/ChapterEditorPage'
import ChaptersPanel from './pages/project/ChaptersPanel'
import CharactersPanel from './pages/project/CharactersPanel'
import OutlinesPanel from './pages/project/OutlinesPanel'
import OverviewPanel from './pages/project/OverviewPanel'
import ProjectLayout from './pages/project/ProjectLayout'
import WizardPage from './pages/project/WizardPage'
import WorldPanel from './pages/project/WorldPanel'

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
      { path: 'settings/ai', element: <AIConfigPage /> },
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
          { path: 'outlines', element: <OutlinesPanel /> },
          { path: 'chapters', element: <ChaptersPanel /> },
          { path: 'chapters/:cid', element: <ChapterEditorPage /> },
        ],
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
])
