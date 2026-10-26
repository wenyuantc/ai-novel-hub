import { Alert, App, Button, Card, Result, Space, Spin, Statistic, Steps, Row, Col } from 'antd'
import { useCallback, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { chapterApi } from '../../api/chapters'
import { errorMessage } from '../../api/client'
import { careerApi } from '../../api/careers'
import { characterApi, outlineApi, worldApi } from '../../api/story'
import type { WizardKind } from '../../api/types'
import WizardGenerator from '../../components/WizardGenerator'
import { useLoader } from '../../hooks/useLoader'
import { useProjectStore } from '../../stores/project'
import { useProjectId } from '../../hooks/useProjectId'

const STEPS: { kind: WizardKind; title: string; description: string }[] = [
  { kind: 'world', title: '世界观', description: '时代背景、力量体系、势力与规则' },
  { kind: 'careers', title: '职业体系', description: '职业与等级阶梯（可跳过）' },
  { kind: 'characters', title: '角色', description: '主要角色卡与人物关系' },
  { kind: 'outline', title: '大纲', description: '分卷分章的故事大纲' },
]

interface Counts {
  world: number
  careers: number
  characters: number
  outline: number
  chapters: number
}

/** 创作向导：世界观 → 职业体系（可跳过）→ 角色 → 大纲 → 批量建章。 */
export default function WizardPage() {
  const pid = useProjectId()
  const { message } = App.useApp()
  const navigate = useNavigate()
  const refreshProject = useProjectStore((s) => s.refresh)
  const [searchParams] = useSearchParams()
  const location = useLocation()
  // 任务面板「跳转」带 ?kind=world|careers|characters|outline：直接打开对应步骤（生成器挂载后自动接上活动任务）
  const kindStep = STEPS.findIndex((s) => s.kind === searchParams.get('kind'))
  const [step, setStep] = useState(() => Math.max(0, kindStep))
  // 页面已打开时再次跳转（含跳到同一 ?kind=）：每次导航（location.key 变化）都按 kind 切换步骤。
  // 在渲染期调整状态（React 推荐写法），切换后旧生成器卸载只断开订阅，任务在后台继续
  const [syncedKey, setSyncedKey] = useState(location.key)
  if (syncedKey !== location.key) {
    setSyncedKey(location.key)
    if (kindStep >= 0) setStep(kindStep)
  }
  const [running, setRunning] = useState(false)
  const [creating, setCreating] = useState(false)

  const loader = useCallback(async (): Promise<Counts> => {
    const [w, cr, c, o, ch] = await Promise.all([
      worldApi.list(pid),
      careerApi.list(pid),
      characterApi.list(pid),
      outlineApi.list(pid),
      chapterApi.list(pid),
    ])
    return { world: w.length, careers: cr.length, characters: c.length, outline: o.length, chapters: ch.length }
  }, [pid])
  const { data: counts, loading, reload } = useLoader(loader)

  const isFinish = step >= STEPS.length
  const current = STEPS[Math.min(step, STEPS.length - 1)]

  const createChapters = async () => {
    setCreating(true)
    try {
      const created = await chapterApi.fromOutlines(pid, null)
      message.success(created.length > 0 ? `已按大纲创建 ${created.length} 个章节` : '大纲条目均已建章')
      void refreshProject()
      navigate(`/projects/${pid}/chapters`)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setCreating(false)
    }
  }

  return (
    <Card title="创作向导">
      <Steps
        current={step}
        onChange={(s) => !running && setStep(s)}
        style={{ marginBottom: 24 }}
        items={[
          ...STEPS.map((s) => ({ title: s.title, description: s.description, disabled: running })),
          { title: '完成', description: '按大纲创建章节', disabled: running },
        ]}
      />
      <Spin spinning={loading && !counts}>
        {isFinish ? (
          <Result
            status="success"
            title="设定准备就绪"
            subTitle="可以按大纲批量创建章节，然后在章节编辑器中逐章生成正文。"
            extra={
              <Space direction="vertical" size="large">
                <Row gutter={32} justify="center">
                  <Col>
                    <Statistic title="世界观条目" value={counts?.world ?? 0} />
                  </Col>
                  <Col>
                    <Statistic title="职业" value={counts?.careers ?? 0} />
                  </Col>
                  <Col>
                    <Statistic title="角色" value={counts?.characters ?? 0} />
                  </Col>
                  <Col>
                    <Statistic title="大纲章节" value={counts?.outline ?? 0} />
                  </Col>
                  <Col>
                    <Statistic title="已建章节" value={counts?.chapters ?? 0} />
                  </Col>
                </Row>
                <Space>
                  <Button onClick={() => setStep(STEPS.length - 1)}>上一步</Button>
                  <Button
                    type="primary"
                    loading={creating}
                    disabled={!counts || counts.outline === 0}
                    onClick={() => void createChapters()}
                  >
                    按大纲批量创建章节
                  </Button>
                  <Button onClick={() => navigate(`/projects/${pid}/overview`)}>进入工作区</Button>
                </Space>
              </Space>
            }
          />
        ) : (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            {current.kind === 'characters' && (counts?.careers ?? 0) > 0 && (
              <Alert
                type="info"
                showIcon
                message={`项目已有 ${counts?.careers} 个职业：生成的角色会按名称挂接主职业 / 副职业与当前阶段，对不上的会在结果中提示。`}
              />
            )}
            <WizardGenerator
              key={current.kind}
              projectId={pid}
              kind={current.kind}
              existingCount={counts?.[current.kind] ?? 0}
              showResult
              onRunningChange={setRunning}
              onDone={() => reload()}
            />
            <Space>
              {step > 0 && (
                <Button disabled={running} onClick={() => setStep(step - 1)}>
                  上一步
                </Button>
              )}
              <Button disabled={running} onClick={() => setStep(step + 1)}>
                跳过
              </Button>
              <Button
                type="primary"
                disabled={running || (counts?.[current.kind] ?? 0) === 0}
                onClick={() => setStep(step + 1)}
              >
                下一步
              </Button>
            </Space>
          </Space>
        )}
      </Spin>
    </Card>
  )
}
