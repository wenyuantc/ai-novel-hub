import { describe, expect, it } from 'vitest'
import { sectionDisclosure, sectionSelectionProblem, type SectionRewriteTarget } from './RichSectionRewriteModalModel'

const target: SectionRewriteTarget = {
  projectId: 1, chapterId: 2, sceneId: 3, sectionKey: 'section', sectionTitle: '备注', includeInAi: false,
  contentRevision: 4, planningRevision: 5, start: 8, end: 10, selectedText: '甲🙂',
}

describe('区块 AI 选区的确认前校验', () => {
  it('位置是码点且非空，和 UTF-16 单元数不同', () => {
    expect(sectionSelectionProblem(target)).toBeNull()
    expect(sectionSelectionProblem({ ...target, end: 11 })).toContain('失效')
    expect(sectionSelectionProblem({ ...target, start: -1 })).toContain('失效')
    expect(sectionSelectionProblem({ ...target, start: 1.5 })).toContain('失效')
  })

  it('上限与 Python 空白语义一致，不错误拒绝 BOM', () => {
    expect(sectionSelectionProblem({ ...target, start: 0, end: 5000, selectedText: '🙂'.repeat(5000) })).toBeNull()
    expect(sectionSelectionProblem({ ...target, start: 0, end: 5001, selectedText: '🙂'.repeat(5001) })).toContain('5000')
    expect(sectionSelectionProblem({ ...target, start: 0, end: 2, selectedText: '\u001c\u0085' })).toContain('选择')
    expect(sectionSelectionProblem({ ...target, start: 0, end: 1, selectedText: '\ufeff' })).toBeNull()
  })

  it('区分关闭块的选区例外和已开启块的自动背景', () => {
    expect(sectionDisclosure(false)).toContain('不自动附带其余内容或名称')
    expect(sectionDisclosure(false)).toContain('其他已关闭区块')
    expect(sectionDisclosure(true)).toContain('名称和其他内容也可能')
    expect(sectionDisclosure(true)).not.toContain('不自动附带其余内容或名称')
  })
})
