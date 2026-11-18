import { Result } from 'antd'
import { StudioPage } from '../components/StudioUI'
import UsageLedger from '../components/aiUsage/UsageLedger'
import { useAiUsageSession } from '../components/aiUsage/useAiUsageRequest'
import '../components/aiUsage/aiUsage.css'

const admin = { kind: 'admin' } as const

export default function AdminAIUsagePage() {
  const session = useAiUsageSession('ai_usage.read')
  if (!session) return <Result status="403" title="没有查看全站 AI 用量的权限" />
  return <div className="ai-usage-page"><StudioPage title="全站 AI 用量与费用">
    <UsageLedger key={session.key} scope={admin} session={session} />
  </StudioPage></div>
}
