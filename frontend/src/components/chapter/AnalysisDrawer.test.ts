import { createElement, type ComponentProps, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { Analysis } from '../../api/types'
import type { ChapterAnalysis } from '../../hooks/useChapterAnalysis'
import AnalysisDrawer from './AnalysisDrawer'

// 只验证真实组件的 SSR 文案和派生状态合同；Drawer 改为原位渲染，不把它当作浏览器交互验证。
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual, Drawer: ({ children }: { children: ReactNode }) => createElement('section', null, children) }
})

const detail: Analysis = {
  id: 5, chapter_id: 3, chapter_no: 1, task_id: null, model: 'fake', scores: {}, metrics: {}, stale: false,
  created_at: '2026-11-07T00:00:00', summary: '原章分析', plot_points: [], hooks: [], conflict: {}, emotion: {},
  changes_raw: {}, foreshadow_candidates: [], suggestions: [], warnings: [], changes: [],
}
const analysis: ChapterAnalysis = {
  state: { chapter_id: 3, status: 'analyzed', latest_analysis_id: 5, scores: {}, analyzed_at: null, active_task_id: null },
  status: 'analyzed', latest: detail, version: -1, task: null, running: false, error: null,
  refresh: async () => null, start: async () => {}, cancel: async () => {}, patchChange: () => {},
}
const base: ComponentProps<typeof AnalysisDrawer> = {
  open: true, onClose: () => {}, chapterId: 3, analysis, autoAnalyze: true, analyzeDisabled: false,
  onAnalyze: async () => {}, reviseBlockedReason: '', reviseRunning: false, onRevise: async () => true, compare: null,
}
function text(extra: Partial<ComponentProps<typeof AnalysisDrawer>> = {}) {
  return renderToStaticMarkup(createElement(AnalysisDrawer, { ...base, ...extra }))
    .replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]+>/g, '')
}
const sceneScope = { kind: 'scene', sceneId: 22, sceneTitle: '桥头初遇', sceneOrder: 1 } as const

describe('按建议重写的范围和分析提示', () => {
  it('明确当前场景的稳定身份，留空按场景原字数，不套用章级300字默认下限', () => {
    const rendered = text({ reviseScope: sceneScope })
    expect(rendered).toContain('本次重写范围')
    expect(rendered).toContain('桥头初遇')
    expect(rendered).toContain('#22')
    expect(rendered).toContain('目标总字数（可选）')
    expect(rendered).toContain('留空按当前场景原文字数，可少于 300 字')
    expect(rendered).toContain('不会自动进行整章分析')
    expect(rendered).toContain('可手动重新分析本章')
    expect(rendered).not.toContain('留空 = 原文字数')
  })
  it('多场景整章明确总量只分配给有正文场景，空场景保留', () => {
    const rendered = text({ reviseScope: { kind: 'chapter', sceneCount: 3 } })
    expect(rendered).toContain('整章（保留场景结构）')
    expect(rendered).toContain('留空按各有正文场景各自原文字数')
    expect(rendered).toContain('按有正文场景均分总量，余数按已保存顺序分配')
    expect(rendered).toContain('空场景保留')
  })
  it('章级仅一个场景的默认目标保留 max(原字数,300) 例外，缺省scope仍是章级', () => {
    for (const extra of [{ reviseScope: { kind: 'chapter' as const, sceneCount: 1 } }, {}]) {
      const rendered = text(extra)
      expect(rendered).toContain('整章（保留场景结构）')
      expect(rendered).toContain('整章仅一个场景时，留空按原文字数与 300 字中的较大值')
    }
  })
  it('运行提示由真实Scene任务决定，不能由当前表单选择伪装成整章或承诺自动分析', () => {
    const rendered = text({ reviseScope: { kind: 'chapter', sceneCount: 3 }, reviseRunning: true, reviseTask: { kind: 'chapter.revise', scene_id: 22 } })
    expect(rendered).toContain('正在按建议重写场景 #22')
    expect(rendered).toContain('不会自动进行整章分析')
    expect(rendered).not.toContain('整章（保留场景结构）')
    expect(rendered).not.toContain('完成后正文自动覆盖并重新分析')
  })
  it('真实章级运行范围覆盖场景表单选择，自动分析关闭时不承诺重新分析', () => {
    const rendered = text({ reviseScope: sceneScope, reviseRunning: true, reviseTask: { kind: 'chapter.revise', scene_id: null }, autoAnalyze: false })
    expect(rendered).toContain('正在按建议重写整章')
    expect(rendered).toContain('已关闭自动分析')
    expect(rendered).toContain('本次重写范围：整章（保留场景结构）')
    expect(rendered).not.toContain('桥头初遇')
    expect(rendered).not.toContain('完成后正文自动覆盖并重新分析')
  })
  it('章级比较仅在实际分析运行时称正在分析，未启动时保留手动分析入口提示', () => {
    const rendered = text({ compare: { baseAnalysisId: 5, sinceAnalysisId: 5 }, autoAnalyze: true })
    expect(rendered).toContain('等待新的整章分析')
    expect(rendered).not.toContain('正在重新分析')
    const running = text({ compare: { baseAnalysisId: 5, sinceAnalysisId: 5 }, analysis: { ...analysis, running: true } })
    expect(running).toContain('正在重新分析')
  })
})
