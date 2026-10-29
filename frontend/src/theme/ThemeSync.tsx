import { useEffect } from 'react'
import { studioPalette, type ResolvedTheme } from './palette'

/** 将共享 palette 同步到 <html> 的主题标记、color-scheme 和 CSS 变量，与 Ant Design 保持一致。 */
export default function ThemeSync({ resolved }: { resolved: ResolvedTheme }) {
  useEffect(() => {
    const colors = studioPalette(resolved)
    const root = document.documentElement
    root.dataset.theme = resolved
    root.style.colorScheme = resolved
    for (const [name, value] of Object.entries(colors)) root.style.setProperty(`--color-${name}`, value)
    root.style.setProperty('--app-bg', colors.surface)
    root.style.setProperty('--app-text', colors['surface-foreground'])
  }, [resolved])
  return null
}
