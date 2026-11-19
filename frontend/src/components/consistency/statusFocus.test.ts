import { describe, expect, it } from 'vitest'
import { statusFocusTarget } from './statusFocus'

type FakeRadio = { value: string; checked: boolean }

/** 按文档顺序模拟 querySelector：选择器列表返回第一个匹配任一选择器的元素。 */
function fakeRoot(radios: FakeRadio[]) {
  const matches = (radio: FakeRadio, selector: string) =>
    selector === 'input[type="radio"]:checked' ? radio.checked : selector === 'input[type="radio"]'
  return {
    querySelector(selectors: string) {
      const list = selectors.split(',').map((item) => item.trim())
      return (radios.find((radio) => list.some((selector) => matches(radio, selector))) ?? null) as unknown as HTMLElement | null
    },
  }
}

describe('statusFocusTarget', () => {
  it('保存后焦点回到当前选中的状态，而不是第一个选项', () => {
    const radios = [
      { value: 'open', checked: false },
      { value: 'resolved', checked: true },
      { value: 'ignored', checked: false },
    ]
    expect(statusFocusTarget(fakeRoot(radios))).toBe(radios[1])
  })

  it('没有选中项时退回第一个选项', () => {
    const radios = [{ value: 'open', checked: false }, { value: 'resolved', checked: false }]
    expect(statusFocusTarget(fakeRoot(radios))).toBe(radios[0])
  })

  it('没有单选时返回 null', () => {
    expect(statusFocusTarget(fakeRoot([]))).toBeNull()
  })
})
