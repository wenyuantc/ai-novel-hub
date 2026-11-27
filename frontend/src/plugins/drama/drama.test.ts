import { describe, expect, it } from 'vitest'
import { canStreamTask, taskKindLabel } from '../../api/tasks'
import { matchRoutes } from 'react-router-dom'
import { PLUGINS, pluginRoutes, withPluginNav } from '../registry'
import { chapterSelectionError, orderedChapterIds } from './chapters'

describe('短剧插件', () => {
  it('登记受启用状态控制的单个导航入口及三级作品路由', () => {
    expect(withPluginNav([], PLUGINS, [], 2)).toEqual([])
    expect(withPluginNav([], PLUGINS, ['drama'], 2)[0].items.map((i) => i.label)).toEqual(['短剧'])
    const routes = pluginRoutes(PLUGINS, 'project')
    expect(matchRoutes(routes, '/features/drama/7/episodes/9')?.[0].params).toEqual({ sid: '7', eid: '9' })
  })
  it('已登记的短剧任务在任务中心显示中文并允许编辑者实时订阅', () => {
    expect(taskKindLabel('drama.rewrite')).toBe('改写短剧剧本')
    expect(canStreamTask('drama.rewrite', true)).toBe(true)
    expect(canStreamTask('drama.unknown', true)).toBe(false)
  })
  it('按作品章节顺序建集，拒绝跳章、缺失章节和超过二十章', () => {
    const chapters = [42, 10, 68, 33].map((id, index) => ({ id, chapter_no: index + 1 }))
    expect(orderedChapterIds(chapters, [68, 10])).toEqual([10, 68])
    expect(chapterSelectionError(chapters, [68, 10])).toBeNull()
    expect(chapterSelectionError(chapters, [42, 68])).toContain('连续')
    expect(chapterSelectionError(chapters, [51])).toContain('章节')
    expect(
      chapterSelectionError(
        chapters,
        Array.from({ length: 21 }, (_, i) => i),
      ),
    ).toContain('20')
  })
})
