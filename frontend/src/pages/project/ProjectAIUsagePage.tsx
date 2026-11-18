import { Result } from 'antd'
import { useCallback, useMemo } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { chapterApi } from '../../api/chapters'
import { StudioPage } from '../../components/StudioUI'
import UsageBudgets from '../../components/aiUsage/UsageBudgets'
import UsageLedger from '../../components/aiUsage/UsageLedger'
import { useAiUsageRequest, useAiUsageSession, type UsageReadOptions, type UsageSession } from '../../components/aiUsage/useAiUsageRequest'
import { parseUsageId } from '../../components/aiUsage/usagePresentation'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore } from '../../stores/project'
import '../../components/aiUsage/aiUsage.css'

function ProjectUsage({ session, projectId, owner, canRead }: {
  session: UsageSession
  projectId: number
  owner: boolean
  canRead: () => boolean
}) {
  const [params] = useSearchParams()
  const initialChapter = parseUsageId(params.get('chapter_id'))
  const scope = useMemo(() => ({ kind: 'project', projectId }) as const, [projectId])
  const loadChapters = useCallback(({ signal, isCurrent }: UsageReadOptions) => chapterApi.list(projectId, signal, isCurrent), [projectId])
  const chapters = useAiUsageRequest(loadChapters, session, canRead)
  const budget = owner ? <UsageBudgets scope={scope} session={session} canAccess={canRead} /> : null
  return <div className="ai-usage-page"><StudioPage title={initialChapter ? '章节 AI 用量与费用' : '作品 AI 用量与费用'}
    action={initialChapter ? <Link to={`/projects/${projectId}/ai-usage`}>查看全书用量</Link> : <Link to="/settings/ai-usage">查看账号预算</Link>}>
    {!initialChapter && budget}
    <UsageLedger key={initialChapter ?? 'all'} scope={scope} session={session} canAccess={canRead} initialChapter={initialChapter} chapters={chapters.data} />
    {initialChapter && budget}
  </StudioPage></div>
}

export default function ProjectAIUsagePage() {
  const projectId = useProjectId()
  const access = useProjectAccess(projectId)
  const session = useAiUsageSession()
  const version = useProjectStore((state) => state.scopeVersion)
  const scope = useMemo(() => {
    const current = session ? captureProjectScope(projectId) : null
    return current?.version === version ? current : null
  }, [session, projectId, version])
  const canRead = useCallback(() => isProjectScopeCurrent(scope), [scope])
  if (!session) return null
  if (!access.canRead || (access.role !== 'owner' && !access.isCollaborator)) return <Result status="403" title="没有查看此作品 AI 用量的权限" />
  return <ProjectUsage key={`${session.key}:${projectId}:${version}`} session={session} projectId={projectId}
    owner={access.role === 'owner'} canRead={canRead} />
}
