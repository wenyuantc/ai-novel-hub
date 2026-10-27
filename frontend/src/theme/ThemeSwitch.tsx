import { DesktopOutlined, MoonOutlined, SunOutlined } from '@ant-design/icons'
import { ConfigProvider, Segmented, theme } from 'antd'
import type { ReactNode } from 'react'
import { useThemeStore, type ThemeMode } from './themeStore'

const OPTIONS: { value: ThemeMode; label: string; icon: ReactNode }[] = [
  { value: 'light', label: '浅色', icon: <SunOutlined /> },
  { value: 'dark', label: '暗色', icon: <MoonOutlined /> },
  { value: 'system', label: '跟随系统', icon: <DesktopOutlined /> },
]

interface Props {
  /** 顶栏（深色背景）里只显示图标；抽屉里显示图标 + 文字 */
  compact?: boolean
  /** 放在深色顶栏上：局部用暗色算法渲染 */
  onDarkBar?: boolean
}

/** 主题三段切换：浅色 / 暗色 / 跟随系统。 */
export default function ThemeSwitch({ compact = false, onDarkBar = false }: Props) {
  const mode = useThemeStore((s) => s.mode)
  const setMode = useThemeStore((s) => s.setMode)
  const control = (
    <Segmented<ThemeMode>
      size={compact ? 'small' : 'middle'}
      block={!compact}
      value={mode}
      onChange={setMode}
      options={OPTIONS.map((o) => ({
        value: o.value,
        icon: o.icon,
        label: compact ? undefined : o.label,
        title: o.label,
      }))}
    />
  )
  return onDarkBar ? <ConfigProvider theme={{ algorithm: theme.darkAlgorithm }}>{control}</ConfigProvider> : control
}
