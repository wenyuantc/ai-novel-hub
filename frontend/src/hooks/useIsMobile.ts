import { Grid } from 'antd'

/** 移动端判断的断点（px）：小于它即为移动端，与 antd 的 md 断点一致。 */
export const MOBILE_BREAKPOINT = 768

/**
 * 是否移动端（视口宽度 < 768px）。基于 antd Grid.useBreakpoint（内部监听窗口尺寸变化），缩放时实时更新。
 * 首次渲染断点尚未计算时（screens 为空）按桌面处理，避免闪烁成移动布局。
 */
export function useIsMobile(): boolean {
  const screens = Grid.useBreakpoint()
  return screens.md === false
}

/** 弹窗 / 抽屉宽度：桌面用期望宽度，移动端占满（留 12px 边距）。 */
export function useModalWidth(desired: number | string): number | string {
  return useIsMobile() ? 'calc(100vw - 24px)' : desired
}
