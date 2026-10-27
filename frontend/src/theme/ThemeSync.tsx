import { theme } from 'antd'
import { useEffect } from 'react'
import type { ResolvedTheme } from './palette'

/** 把当前主题同步到 <html>：data-theme、color-scheme（原生滚动条 / 表单控件）与 index.css 用到的 CSS 变量（取自 antd token）。 */
export default function ThemeSync({ resolved }: { resolved: ResolvedTheme }) {
  const { token } = theme.useToken()
  useEffect(() => {
    const root = document.documentElement
    root.dataset.theme = resolved
    root.style.colorScheme = resolved
    root.style.setProperty('--app-bg', token.colorBgLayout)
    root.style.setProperty('--app-text', token.colorText)
  }, [resolved, token.colorBgLayout, token.colorText])
  return null
}
