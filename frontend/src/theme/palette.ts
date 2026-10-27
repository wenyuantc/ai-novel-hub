// 全站唯一允许写死颜色值的文件（noHardcodedColors.test.ts 白名单）。
// antd 组件内的颜色一律用 theme.useToken()；非 antd 绘制（关系图、伏笔时间线、评分走势、书封、阅读背景）从这里取，分浅 / 暗两套。

export type ResolvedTheme = 'light' | 'dark'

/** 品牌主色（ConfigProvider colorPrimary） */
export const BRAND_PRIMARY = '#1677ff'

export interface ChartPalette {
  primary: string
  success: string
  danger: string
  warning: string
  /** 弱化 / 已废弃 */
  muted: string
  text: string
  textSecondary: string
  /** 网格线 */
  grid: string
  /** 图表底色 */
  surface: string
  /** 节点底色（存活 / 非存活） */
  nodeBg: string
  nodeBgMuted: string
  /** 关系图节点按定位着色 */
  roles: { protagonist: string; villain: string; important: string; supporting: string; other: string }
  /** 评分走势四条线 */
  series: { overall: string; pacing: string; engagement: string; coherence: string }
}

const LIGHT: ChartPalette = {
  primary: '#1677ff',
  success: '#389e0d',
  danger: '#cf1322',
  warning: '#faad14',
  muted: '#bfbfbf',
  text: '#262626',
  textSecondary: '#8c8c8c',
  grid: '#f0f0f0',
  surface: '#fafafa',
  nodeBg: '#ffffff',
  nodeBgMuted: '#f5f5f5',
  roles: { protagonist: '#faad14', villain: '#f5222d', important: '#1677ff', supporting: '#13c2c2', other: '#8c8c8c' },
  series: { overall: '#1677ff', pacing: '#13c2c2', engagement: '#fa8c16', coherence: '#722ed1' },
}

const DARK: ChartPalette = {
  primary: '#3c89e8',
  success: '#6abe39',
  danger: '#e84749',
  warning: '#d89614',
  muted: '#595959',
  text: '#e6e6e6',
  textSecondary: '#8c8c8c',
  grid: '#303030',
  surface: '#1f1f1f',
  nodeBg: '#1f1f1f',
  nodeBgMuted: '#141414',
  roles: { protagonist: '#d89614', villain: '#e84749', important: '#3c89e8', supporting: '#33bcb7', other: '#8c8c8c' },
  series: { overall: '#3c89e8', pacing: '#33bcb7', engagement: '#e89a3c', coherence: '#854eca' },
}

export function chartPalette(resolved: ResolvedTheme): ChartPalette {
  return resolved === 'dark' ? DARK : LIGHT
}

/** 阅读器背景：浅色 / 护眼 / 暗色（「跟随全局」时按全局主题取 light / dark） */
export type ReaderTone = 'light' | 'sepia' | 'dark'
export const READER_TONES: Record<ReaderTone, { bg: string; text: string; secondary: string; dark: boolean }> = {
  light: { bg: '#ffffff', text: '#262626', secondary: '#8c8c8c', dark: false },
  sepia: { bg: '#f5ecd7', text: '#4a3b28', secondary: '#8a7a62', dark: false },
  dark: { bg: '#1b1b1d', text: '#c8c8c8', secondary: '#7a7a7a', dark: true },
}

/** 书封文字色（底色按它压暗到对比度 ≥ 4.5） */
export const COVER_TEXT = { onDark: '#ffffff' } as const
