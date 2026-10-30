import { useAuthStore } from '../stores/auth'
import { useProjectStore } from '../stores/project'
import { projectAccess } from '../utils/projectAccess'

export function useProjectAccess(projectId: number) {
  const user = useAuthStore((s) => s.user)
  const project = useProjectStore((s) => s.project)
  return projectAccess(project, user, projectId)
}
