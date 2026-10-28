import { CloseOutlined, RobotOutlined } from '@ant-design/icons'
import { Button, Drawer, Skeleton, Typography, theme } from 'antd'
import { lazy, Suspense, useEffect, useRef } from 'react'
import { AGENT_DOCK_WIDTH_VAR, AGENT_MAX_WIDTH, AGENT_MIN_WIDTH, clampAgentWidth } from '../../utils/agentPreferences'

const AgentPanel = lazy(() => import('./AgentPanel'))
interface Props { projectId: number; open: boolean; mobile: boolean; width: number; onWidth: (width: number) => void; onClose: () => void }
export default function AgentDock({ projectId, open, mobile, width, onWidth, onClose }: Props) {
  const { token } = theme.useToken()
  const drag = useRef<{ x: number; width: number } | null>(null)
  const docked = open && !mobile
  // 全局任务悬浮按钮通过该变量让出面板宽度，避免压住输入框。
  useEffect(() => {
    if (!docked) return
    const root = document.documentElement
    root.style.setProperty(AGENT_DOCK_WIDTH_VAR, `${width}px`)
    return () => { root.style.removeProperty(AGENT_DOCK_WIDTH_VAR) }
  }, [docked, width])
  const content = open && <Suspense fallback={<div style={{ padding: 16 }}><Skeleton active paragraph={{ rows: 6 }} /></div>}><AgentPanel key={projectId} projectId={projectId} /></Suspense>
  if (mobile) return <Drawer title={<><RobotOutlined /> 项目智能体</>} width="100%" open={open} onClose={onClose} destroyOnHidden styles={{ body: { padding: 0, overflow: 'hidden' } }}>{content}</Drawer>
  if (!open) return null
  return <aside aria-label="项目智能体" style={{ width, maxWidth: 'calc(100% - 240px)', minWidth: AGENT_MIN_WIDTH, flexShrink: 0, position: 'sticky', top: 64, alignSelf: 'flex-start', height: 'calc(100dvh - 64px)', background: token.colorBgContainer, borderInlineStart: `1px solid ${token.colorBorderSecondary}`, display: 'flex', flexDirection: 'column' }}>
    <div role="separator" aria-label="调整智能体面板宽度" aria-orientation="vertical" aria-valuemin={AGENT_MIN_WIDTH} aria-valuemax={AGENT_MAX_WIDTH} aria-valuenow={width} tabIndex={0} style={{ position: 'absolute', insetInlineStart: -4, top: 0, bottom: 0, width: 8, cursor: 'col-resize', touchAction: 'none', zIndex: 1 }} onPointerDown={(e) => {
      drag.current = { x: e.clientX, width }; e.currentTarget.setPointerCapture(e.pointerId)
    }} onPointerMove={(e) => { if (drag.current) onWidth(clampAgentWidth(drag.current.width + drag.current.x - e.clientX)) }} onPointerUp={(e) => { drag.current = null; e.currentTarget.releasePointerCapture(e.pointerId) }} onPointerCancel={() => { drag.current = null }} onKeyDown={(e) => {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); onWidth(clampAgentWidth(width + (e.key === 'ArrowLeft' ? 20 : -20))) }
      if (e.key === 'Home') { e.preventDefault(); onWidth(AGENT_MIN_WIDTH) }
      if (e.key === 'End') { e.preventDefault(); onWidth(AGENT_MAX_WIDTH) }
    }} />
    <div style={{ padding: '8px 12px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: `1px solid ${token.colorBorderSecondary}` }}><Typography.Text strong><RobotOutlined /> 项目智能体</Typography.Text><Button type="text" size="small" icon={<CloseOutlined />} aria-label="收起智能体面板" onClick={onClose} /></div>
    <div style={{ flex: 1, minHeight: 0 }}>{content}</div>
  </aside>
}
