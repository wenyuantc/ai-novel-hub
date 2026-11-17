import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import IncludedDefinitions from './IncludedDefinitions'

describe('模板预览的配套定义', () => {
  it('列出随所选内容保存的字段、选项、分类、关系类型及职业阶段', () => {
    const html = renderToStaticMarkup(createElement(IncludedDefinitions, { definitions: {
      fields: [{ name: '阵营', field_type: 'dropdown', options: ['北境', '南境'] }],
      categories: ['门派'], relationship_types: [{ origin: 'project', category: 'family', role_a: '父亲', role_b: '女儿' }],
      careers: [{ name: '剑修', stages: ['入门', '宗师'] }],
    } }))
    for (const value of ['阵营', '下拉选项', '北境', '南境', '门派', '父亲', '女儿', '剑修', '入门', '宗师']) {
      expect(html).toContain(value)
    }
    expect(html).toContain('作品专用')
    expect(html).toContain('家庭')
  })
})
