import { Link } from 'react-router-dom'
import { projectApi } from '../api/projects'
import { StudioPage } from '../components/StudioUI'
import UsageBudgets from '../components/aiUsage/UsageBudgets'
import UsageLedger from '../components/aiUsage/UsageLedger'
import { useAiUsageRequest, useAiUsageSession, type UsageSession } from '../components/aiUsage/useAiUsageRequest'
import '../components/aiUsage/aiUsage.css'

const account = { kind: 'account' } as const

function AccountUsage({ session }: { session: UsageSession }) {
  const projects = useAiUsageRequest(projectApi.list, session)
  return <div className="ai-usage-page"><StudioPage title="AI 用量与费用" action={<Link to="/settings/ai">配置模型单价</Link>}>
    <UsageBudgets scope={account} session={session} />
    <UsageLedger scope={account} session={session} projects={projects.data} />
  </StudioPage></div>
}

export default function AIUsagePage() {
  const session = useAiUsageSession()
  return session ? <AccountUsage key={session.key} session={session} /> : null
}
