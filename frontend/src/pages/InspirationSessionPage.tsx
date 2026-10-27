import { ArrowLeftOutlined, ProfileOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Col, Drawer, Result, Row, Space, Spin, Steps, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { inspirationApi } from '../api/inspiration'
import type { InspirationCurrent, InspirationSession } from '../api/types'
import ConfirmStep from '../components/inspiration/ConfirmStep'
import DraftSidebar from '../components/inspiration/DraftSidebar'
import IdeaStep from '../components/inspiration/IdeaStep'
import OptionStep from '../components/inspiration/OptionStep'
import ScaleStep from '../components/inspiration/ScaleStep'
import { useIsMobile } from '../hooks/useIsMobile'
import { useLoader } from '../hooks/useLoader'
import {
  FLOW_LABELS,
  INSPIRATION_FLOW,
  isStepKey,
  nextFlowStep,
  SESSION_STATUS,
  STEP_DISPLAY,
  stepDisplayStatus,
} from '../utils/inspiration'

/** 灵感会话页（/inspiration/:sid）：左侧步骤条、中间当前步骤、右侧立项草稿与补充想法；移动端单栏，步骤条横向滚动、草稿收进抽屉。 */
export default function InspirationSessionPage() {
  const { sid } = useParams()
  const id = Number(sid)
  const loader = useCallback(() => inspirationApi.get(id), [id])
  const { data, loading, error } = useLoader(loader)

  if (loading && !data) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Spin />
      </div>
    )
  }
  if (error || !data) {
    return (
      <Result
        status="404"
        title="灵感会话不存在"
        subTitle={error ?? undefined}
        extra={
          <Link to="/inspiration">
            <Button type="primary">返回会话列表</Button>
          </Link>
        }
      />
    )
  }
  return <SessionView key={data.id} initial={data} />
}

function SessionView({ initial }: { initial: InspirationSession }) {
  const { message } = App.useApp()
  const [session, setSession] = useState(initial)
  // 当前步骤以本地状态为准（切换时异步保存到会话），避免其他请求的响应把它改回旧值
  const [current, setCurrent] = useState<InspirationCurrent>(initial.state.current)
  const [running, setRunning] = useState(false)
  const isMobile = useIsMobile()
  const [draftOpen, setDraftOpen] = useState(false)
  const state = session.state

  const goTo = (key: InspirationCurrent) => {
    if (running) {
      message.warning('正在生成选项，请先停止或等待完成')
      return
    }
    if (key === current) return
    setCurrent(key)
    inspirationApi.update(session.id, { current: key }).catch(() => {
      // 只影响下次打开时停在哪一步，忽略
    })
  }

  const advance = (s: InspirationSession) => goTo(nextFlowStep(s.state, current))
  const prev = () => {
    const idx = INSPIRATION_FLOW.indexOf(current)
    if (idx > 0) goTo(INSPIRATION_FLOW[idx - 1])
  }

  const renderStep = () => {
    if (current === 'idea') return <IdeaStep session={session} onSession={setSession} onNext={advance} />
    if (current === 'scale') return <ScaleStep session={session} onSession={setSession} onNext={() => goTo('confirm')} onPrev={prev} />
    if (current === 'confirm') return <ConfirmStep session={session} onSession={setSession} onPrev={prev} />
    if (isStepKey(current))
      return (
        <OptionStep
          key={current}
          session={session}
          step={current}
          onSession={setSession}
          onAdvance={advance}
          onPrev={prev}
          onRunningChange={setRunning}
        />
      )
    return null
  }

  const stepItems = INSPIRATION_FLOW.map((key) => {
    const st = stepDisplayStatus(state, key)
    return {
      title: FLOW_LABELS[key],
      status: (key === current ? 'process' : st === 'decided' || st === 'skipped' ? 'finish' : 'wait') as 'process' | 'finish' | 'wait',
      description:
        key === 'confirm' ? undefined : (
          <Tag color={STEP_DISPLAY[st].color} style={{ marginTop: 2 }}>
            {STEP_DISPLAY[st].label}
          </Tag>
        ),
    }
  })

  return (
    <div style={{ maxWidth: 1440, margin: '0 auto', padding: isMobile ? 12 : 24 }}>
      <Space style={{ marginBottom: 16 }} wrap>
        <Link to="/inspiration">
          <Button type="text" icon={<ArrowLeftOutlined />}>
            灵感会话
          </Button>
        </Link>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {session.title || '灵感会话'}
        </Typography.Title>
        <Tag color={SESSION_STATUS[session.status].color}>{SESSION_STATUS[session.status].label}</Tag>
        {session.converted_project_id !== null && <Link to={`/projects/${session.converted_project_id}`}>打开转为的项目</Link>}
        {session.project_id !== null && <Link to={`/projects/${session.project_id}`}>来源项目</Link>}
      </Space>
      {session.project_id !== null && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="从已有项目发起：项目里已有的字段已预填为「已定」，只需为空着的步骤生成选项；完成后在「确认」里应用回项目（默认只填空字段）。"
        />
      )}
      {isMobile ? (
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <div style={{ overflowX: 'auto', paddingBottom: 4 }}>
            <Steps
              size="small"
              responsive={false}
              current={INSPIRATION_FLOW.indexOf(current)}
              onChange={(i) => goTo(INSPIRATION_FLOW[i])}
              items={stepItems}
              style={{ minWidth: INSPIRATION_FLOW.length * 110 }}
            />
          </div>
          <Button icon={<ProfileOutlined />} block onClick={() => setDraftOpen(true)}>
            立项草稿与补充想法
          </Button>
          <Card styles={{ body: { padding: 12 } }}>{renderStep()}</Card>
          <Drawer title="立项草稿" placement="bottom" height="80vh" open={draftOpen} onClose={() => setDraftOpen(false)} forceRender>
            <DraftSidebar
              session={session}
              onSession={setSession}
              onGoTo={(key) => {
                setDraftOpen(false)
                goTo(key)
              }}
              disabled={running}
            />
          </Drawer>
        </Space>
      ) : (
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={5}>
            <Card size="small">
              <Steps
                direction="vertical"
                size="small"
                current={INSPIRATION_FLOW.indexOf(current)}
                onChange={(i) => goTo(INSPIRATION_FLOW[i])}
                items={stepItems}
              />
            </Card>
          </Col>
          <Col xs={24} lg={12}>
            <Card>{renderStep()}</Card>
          </Col>
          <Col xs={24} lg={7}>
            <DraftSidebar session={session} onSession={setSession} onGoTo={goTo} disabled={running} />
          </Col>
        </Row>
      )}
    </div>
  )
}
