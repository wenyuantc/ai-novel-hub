// 伏笔状态标签：单独成模块，供入口 chunk 中的 api/analysis（回写清单展示）引用，
// 避免把整份 api/foreshadows（表单 / 时间线 / 提醒等）拉进入口 chunk。

import type { ForeshadowStatus } from './types'

export const FORESHADOW_STATUSES: ForeshadowStatus[] = ['pending', 'planted', 'resolved', 'partial', 'abandoned']

export const FORESHADOW_STATUS: Record<ForeshadowStatus, { label: string; color: string }> = {
  pending: { label: '待埋', color: 'default' },
  planted: { label: '已埋', color: 'blue' },
  resolved: { label: '已回收', color: 'success' },
  partial: { label: '部分回收', color: 'cyan' },
  abandoned: { label: '已废弃', color: 'default' },
}

export function foreshadowStatusLabel(s: string): { label: string; color: string } {
  return s in FORESHADOW_STATUS ? FORESHADOW_STATUS[s as ForeshadowStatus] : { label: s || '-', color: 'default' }
}
