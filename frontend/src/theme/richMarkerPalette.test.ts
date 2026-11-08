import { describe, expect, it } from 'vitest'
import { richMarkerPalette, studioPalette } from './palette'

function hex(value: string): number[] {
  const matched = /^#([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(value)
  if (!matched) throw new Error(`不支持的颜色 ${value}`)
  return matched.slice(1).map((part) => Number.parseInt(part, 16))
}

function composite(value: string, background: number[]): number[] {
  const matched = /^rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)$/.exec(value)
  if (!matched) throw new Error(`不支持的叠色 ${value}`)
  const color = matched.slice(1, 4).map(Number)
  const alpha = Number(matched[4])
  return color.map((component, index) => component * alpha + background[index] * (1 - alpha))
}

function luminance(value: number[]): number {
  const [red, green, blue] = value.map((component) => {
    const channel = component / 255
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue
}

describe('正文作者标记的明暗调色', () => {
  it.each(['light', 'dark'] as const)('%s 下五种叠色不同且正文对比度不少于 4.5', (mode) => {
    const theme = studioPalette(mode)
    const background = hex(theme['surface-raised'])
    const text = luminance(hex(theme['surface-foreground']))
    const colors = Object.values(richMarkerPalette(mode)).map((value) => composite(value, background))
    expect(new Set(colors.map((value) => value.map(Math.round).join(','))).size).toBe(5)
    for (const color of colors) {
      const surface = luminance(color)
      expect((Math.max(text, surface) + 0.05) / (Math.min(text, surface) + 0.05)).toBeGreaterThanOrEqual(4.5)
    }
  })
})
