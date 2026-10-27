import { chartPalette, type ChartPalette } from './palette'
import { useResolvedTheme } from './themeStore'

/** 非 antd 绘制（SVG / 图谱）的配色，随全局主题切换。 */
export function usePalette(): ChartPalette {
  return chartPalette(useResolvedTheme())
}
