export const AGENT_MIN_WIDTH = 320
export const AGENT_MAX_WIDTH = 720
/** 桌面停靠面板打开时写在根元素上的宽度变量，供全局悬浮按钮避让。 */
export const AGENT_DOCK_WIDTH_VAR = '--agent-dock-width'
const STORAGE_KEY = 'ainovel.agentPanel'
export interface AgentPanelPreferences { open: boolean; width: number }
export function clampAgentWidth(value: number): number {
  return Number.isFinite(value) ? Math.round(Math.min(AGENT_MAX_WIDTH, Math.max(AGENT_MIN_WIDTH, value))) : 400
}
export function readAgentPreferences(): AgentPanelPreferences {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}')
    if (typeof value === 'object' && value !== null) {
      const v = value as Record<string, unknown>
      return { open: v.open === true, width: clampAgentWidth(typeof v.width === 'number' ? v.width : 400) }
    }
  } catch { /* 设备禁止存储时使用默认值 */ }
  return { open: false, width: 400 }
}
export function writeAgentPreferences(value: AgentPanelPreferences): void {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(value)) } catch { /* 偏好存储失败不影响使用 */ }
}
