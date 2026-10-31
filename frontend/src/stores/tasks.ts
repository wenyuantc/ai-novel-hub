import { create } from 'zustand'
import { isActiveTask, isOpenTask, taskApi } from '../api/tasks'
import type { Task } from '../api/types'

/** 有活动任务时的轮询间隔 */
export const POLL_ACTIVE_MS = 3000
/** 没有活动任务时的轮询间隔 */
export const POLL_IDLE_MS = 15000

export function pollInterval(activeCount: number): number {
  return activeCount > 0 ? POLL_ACTIVE_MS : POLL_IDLE_MS
}

/** 列表中真正活动（排队 / 运行中）的任务数；待审核不推进，不需要快速轮询 */
export function countRunning(list: Task[]): number {
  return list.filter((t) => isActiveTask(t.status)).length
}

/** 把一个任务合并进未结束列表（含待审核）：已结束则移除，否则按 id 覆盖/插入；保持 id 倒序（与后端列表一致）。 */
export function upsertActive(list: Task[], task: Task): Task[] {
  const rest = list.filter((t) => t.id !== task.id)
  if (!isOpenTask(task.status)) return rest
  return [...rest, task].sort((a, b) => b.id - a.id)
}

/** 上一轮存在、本轮已不在列表中的任务数（即刚结束的任务数），加上刚从活动转为待审核的任务数 */
export function countFinished(prev: Task[], next: Task[]): number {
  const byId = new Map(next.map((t) => [t.id, t]))
  return prev.filter((t) => {
    const now = byId.get(t.id)
    if (!now) return true
    return isActiveTask(t.status) && now.status === 'paused'
  }).length
}

interface TasksState {
  /** 当前用户未结束的任务：排队 + 运行中 + 待审核（id 倒序） */
  active: Task[]
  loaded: boolean
  /** 每当有活动任务结束（从列表中消失）/ 转为待审核，或被本地删除/清理时递增，供「最近」列表等重新加载 */
  version: number
  /** 拉取活动任务并按新节奏安排下一次轮询 */
  refresh: () => Promise<void>
  /** 本地创建 / 重试 / 取消任务后调用：立即合并并刷新（切到 3 秒节奏） */
  track: (task: Task) => void
  /** 本地删除 / 清理已结束任务后调用 */
  bump: () => void
  /** 目标作品删除/恢复后清理本地旧任务，并使已发出的轮询失效。 */
  discardProject: (projectId: number) => void
  /** 开始轮询（引用计数），返回停止函数 */
  startPolling: () => () => void
  reset: () => void
}

let pollers = 0
let timer: ReturnType<typeof setTimeout> | null = null
// 每次请求 / reset 递增：丢弃过期响应（例如切换用户后到达的上一个用户的结果）
let requestSeq = 0
let epoch = 0
const discardedProjects = new Set<number>()

export const useTasksStore = create<TasksState>((set, get) => {
  const schedule = () => {
    if (timer) clearTimeout(timer)
    timer = null
    if (pollers <= 0) return
    timer = setTimeout(() => void get().refresh(), pollInterval(countRunning(get().active)))
  }

  return {
    active: [],
    loaded: false,
    version: 0,

    refresh: async () => {
      const seq = ++requestSeq
      const myEpoch = epoch
      try {
        const list = await taskApi.list({ active: true, limit: 200 })
        if (seq !== requestSeq || myEpoch !== epoch) return
        // 后端新响应是恢复后新任务的权威来源；迟到的本地 track 不能自行重新挂回旧任务。
        for (const task of list) if (task.project_id !== null) discardedProjects.delete(task.project_id)
        const prev = get().active
        const finished = countFinished(prev, list)
        set({ active: list, loaded: true, version: finished > 0 ? get().version + 1 : get().version })
      } catch {
        // 轮询失败静默（401 由全局处理器负责登出）
      } finally {
        if (seq === requestSeq && myEpoch === epoch) schedule()
      }
    },

    track: (task) => {
      if (task.project_id !== null && discardedProjects.has(task.project_id)) { void get().refresh(); return }
      set({ active: upsertActive(get().active, task), version: get().version + 1 })
      void get().refresh()
    },

    bump: () => set({ version: get().version + 1 }),

    discardProject: (projectId) => {
      ++requestSeq
      discardedProjects.add(projectId)
      set({ active: get().active.filter((task) => task.project_id !== projectId), version: get().version + 1 })
      void get().refresh()
    },

    startPolling: () => {
      pollers += 1
      void get().refresh()
      let stopped = false
      return () => {
        if (stopped) return
        stopped = true
        pollers -= 1
        if (pollers <= 0 && timer) {
          clearTimeout(timer)
          timer = null
        }
      }
    },

    reset: () => {
      epoch += 1
      discardedProjects.clear()
      if (timer) clearTimeout(timer)
      timer = null
      set({ active: [], loaded: false, version: get().version + 1 })
      // 仍有轮询者（例如登录后 AppLayout 先于 App 的重置 effect 挂载）：用新会话重新开始
      if (pollers > 0) void get().refresh()
    },
  }
})
