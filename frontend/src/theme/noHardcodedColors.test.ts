import { describe, expect, it } from 'vitest'

// 防回归：源码中禁止写死颜色值（#hex / rgb() / rgba() / hsl() / hsla()），请改用 antd token（theme.useToken）或 theme/palette.ts。
// 白名单：palette.ts 整个文件；确有必要的单行在行尾加 `// theme-allow` 注释。测试文件不扫描。
const sources = import.meta.glob(['../**/*.{ts,tsx,css}', '!../**/*.test.ts'], {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

const WHITELIST = new Set(['./palette.ts', '../theme/palette.ts'])
const COLOR_RE = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(/

function findHardcodedColors(files: Record<string, string>): string[] {
  const hits: string[] = []
  for (const [path, text] of Object.entries(files)) {
    if (WHITELIST.has(path)) continue
    text.split('\n').forEach((line, i) => {
      if (line.includes('theme-allow')) return
      if (COLOR_RE.test(line)) hits.push(`${path.replace(/^\.\.\//, 'src/')}:${i + 1}  ${line.trim()}`)
    })
  }
  return hits
}

describe('禁止写死颜色', () => {
  it('扫描到了源码文件', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50)
    expect(Object.keys(sources)).toContain('../App.tsx')
    expect(Object.keys(sources)).toContain('../index.css')
    // Skill / MCP 页面与组件同样受检
    for (const file of ['../pages/SkillsPage.tsx', '../pages/McpPluginsPage.tsx', '../components/mcp/McpPluginDrawer.tsx', '../components/skills/SkillEditDrawer.tsx'])
      expect(Object.keys(sources)).toContain(file)
  })

  it('除白名单外没有写死的颜色值', () => {
    expect(findHardcodedColors(sources)).toEqual([])
  })

  it('能识别各种写法，theme-allow 行放行', () => {
    const hits = findHardcodedColors({
      'a.tsx': "const a = '#fff'\nconst b = 'rgba(0,0,0,.1)'\nconst c = '#1677ff' // theme-allow\nconst d = `#${id}`\nconst e = 'hsl(10, 50%, 50%)'",
    })
    expect(hits).toHaveLength(3)
  })
})
