import type { CSSProperties } from 'react'

/**
 * 按容器宽度排布的两栏 / 多栏布局（flex 换行）：项目页内容区会被右侧停靠面板挤窄，宽度与视口无关，
 * 因此不用 `<Col xl>` 这类视口断点。各栏基准宽度之和（含间距）放不下时自动上下堆叠。
 */
export const splitRow: CSSProperties = { display: 'flex', flexWrap: 'wrap', gap: 16 }

/** 分栏中的一栏：基准宽度 `basis`（px），剩余空间按基准宽度等比分配，保持各栏比例。 */
export function splitPane(basis: number): CSSProperties {
  return { flex: `${basis} 1 ${basis}px`, minWidth: 0 }
}
