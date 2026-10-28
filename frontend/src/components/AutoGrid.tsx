import type { CSSProperties, ReactNode } from 'react'
import { useIsMobile } from '../hooks/useIsMobile'

interface Props {
  children: ReactNode
  /** 每项最小宽度（px），容器放得下几列就排几列；容器比它还窄时收缩到 100%，不横向溢出。 */
  minItemWidth?: number
  /** 行 / 列间距，同 CSS `gap`。 */
  gap?: CSSProperties['gap']
  /** 移动端（< 768px）固定列数，默认单列。 */
  mobileColumns?: number
  style?: CSSProperties
}

/** 按容器宽度自适应列数的网格（CSS grid auto-fill），不按视口断点分列——项目页右侧停靠面板打开、内容区变窄时卡片不会被挤压。 */
export default function AutoGrid({ children, minItemWidth = 260, gap = 16, mobileColumns = 1, style }: Props) {
  const isMobile = useIsMobile()
  const columns = isMobile
    ? `repeat(${mobileColumns}, minmax(0, 1fr))`
    : `repeat(auto-fill, minmax(min(${minItemWidth}px, 100%), 1fr))`
  return <div style={{ display: 'grid', gridTemplateColumns: columns, gap, ...style }}>{children}</div>
}
