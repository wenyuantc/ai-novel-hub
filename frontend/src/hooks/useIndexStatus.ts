import { useCallback, useEffect, useState } from 'react'
import { kbApi } from '../api/kb'
import { isActiveTask } from '../api/tasks'
import type { IndexStatus } from '../api/types'
import { useTasksStore } from '../stores/tasks'
import { startVisiblePolling, type PollingVisibility } from '../utils/visiblePolling'

/** 建索引进行中时的刷新间隔 */
export const INDEX_POLL_MS = 3000

interface Options {
  busy?: boolean
  onTick?: () => void
  enabled?: boolean
  /**
   * 协作者：`kb.index` 实时订阅对协作者为 404（裁决 5），进度改为只在页面可见时轮询 index-status
   * （`startVisiblePolling`，回到前台立即刷新一次），建索引结束后停止。作者保持原定时刷新。
   */
  foreground?: boolean
  /** 变化即重新读取（如协作授权轮次 / 模型授权变化后，服务端的检索模式可能随之改变） */
  revalidateKey?: string
  /** 测试注入；默认 `document` */
  visibility?: PollingVisibility
}

/**
 * 建索引进度的刷新节奏：作者原样每 3 秒定时刷新；协作者（foreground）只在页面可见时刷新、
 * 回到前台立即刷新一次。没有可用的 visibility（非浏览器）时协作者不轮询。返回值完整清理。
 */
export function startIndexStatusPolling(
  tick: () => void, foreground: boolean, visibility: PollingVisibility | undefined, interval = INDEX_POLL_MS,
): (() => void) | undefined {
  if (foreground) return visibility ? startVisiblePolling(tick, visibility, interval) : undefined
  const timer = setInterval(tick, interval)
  return () => clearInterval(timer)
}

/** 已读到的状态及其读取键（作品 + revalidateKey）。 */
export interface KeyedIndexStatus {
  key: string
  status: IndexStatus
}

export function indexStatusKey(pid: number, revalidateKey: string): string {
  return `${pid}|${revalidateKey}`
}

/**
 * 只展示与当前读取键一致的状态：作品或授权轮次 / 模型授权变化后，新状态读到之前不沿用旧结果
 * （避免作者刚关闭授权时仍显示「向量可用」的旧横幅与重建入口）；关闭时一律不返回。
 */
export function currentIndexStatus(loaded: KeyedIndexStatus | undefined, key: string, enabled: boolean): IndexStatus | undefined {
  return enabled && loaded?.key === key ? loaded.status : undefined
}

/**
 * 项目索引状态（知识库页 / 记忆页共用）：挂载时加载；任务结束（store `version` 变化）后刷新；
 * 本项目有活动 `kb.index` 任务、后端报告 `indexing` 或调用方报告 `busy`（如有文档在处理）时每 3 秒刷新，并调用 `onTick`。
 */
export function useIndexStatus(pid: number, { busy = false, onTick, enabled = true, foreground = false, revalidateKey = '', visibility }: Options = {}) {
  const key = indexStatusKey(pid, revalidateKey)
  const [loaded, setLoaded] = useState<KeyedIndexStatus>()
  const status = currentIndexStatus(loaded, key, enabled)
  const version = useTasksStore((s) => s.version)
  const kbActive = useTasksStore((s) =>
    s.active.some((t) => t.kind === 'kb.index' && t.project_id === pid && isActiveTask(t.status)),
  )

  // 递增即重新读取（刷新 / 轮询）；读取放在 effect 的回调里，避免在 effect 中同步 setState
  const [tick, setTick] = useState(0)
  const refresh = useCallback(() => { if (enabled) setTick((t) => t + 1) }, [enabled])

  useEffect(() => {
    if (!enabled) return
    let alive = true
    kbApi.indexStatus(pid).then(
      (s) => {
        if (alive) setLoaded({ key, status: s })
      },
      () => {
        // 状态只用于横幅提示，读取失败不打扰用户（页面其他请求会显示错误）
      },
    )
    return () => {
      alive = false
    }
  }, [pid, version, tick, enabled, key])

  const polling = enabled && (busy || kbActive || Boolean(status?.indexing))
  useEffect(() => {
    if (!polling) return
    const tickOnce = () => {
      refresh()
      onTick?.()
    }
    return startIndexStatusPolling(tickOnce, foreground, visibility ?? (typeof document === 'undefined' ? undefined : document))
  }, [polling, refresh, onTick, foreground, visibility])

  // 关闭（如降为查看者）或读取键变化后不再展示旧状态
  return { status, refresh, polling, version }
}
