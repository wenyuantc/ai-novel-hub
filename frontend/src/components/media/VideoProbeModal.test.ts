import { createElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import type { AIConfig, Task } from '../../api/types'
import VideoProbeModal from './VideoProbeModal'

const history = vi.hoisted(() => ({ latest: null as Task | null }))
vi.mock('antd', async (original) => ({
  ...await original<typeof import('antd')>(),
  Modal: ({ children }: { children: ReactNode }) => children,
}))
vi.mock('../aiUsage/useAiUsageRequest', () => ({
  useAiUsageRequest: () => ({ loading: false, data: history.latest }),
  useAiUsageMutation: () => ({ loading: false }),
}))

const config: AIConfig = { id: 2, name: '视频测试模型', provider: 'minimax', model_type: 'video', model: 'video-01',
  base_url: null, api_key_masked: '***', is_default: true, thinking_level: 'off', context_window_k: null,
  max_output_k: null, created_at: '', updated_at: '' }
const previous: Task = { id: 9, task_key: '00000000-0000-4000-8000-000000000009', status: 'running', progress: 67,
  project_id: null, chapter_id: null, kind: 'ai.video_probe', title: '测试', stage: '正在查询', error: '', retry_of_id: null,
  character_id: null, analysis_id: null, outline_id: null, checkpoint: null, step_cursor: 0, queue_position: null,
  created_at: '', started_at: null, finished_at: null }
function view(latest: Task | null) {
  history.latest = latest
  return renderToStaticMarkup(createElement(VideoProbeModal, { config, onClose: () => {},
    session: { key: 'actor', actor: { id: 2, createdAt: '' }, current: () => true } }))
}

it('首次测试提供可选参考图并提醒费用', () => {
  const html = view(null)
  expect(html).toContain('type="file"')
  expect(html).toContain('开始生成')
  expect(html).toContain('计入个人 AI 用量与费用')
})

it('重新打开运行中的测试只提供取消并展示进度', () => {
  const html = view(previous)
  expect(html).toContain('正在查询')
  expect(html).toContain('67%')
  expect(html).toContain('取消生成')
  expect(html).not.toContain('开始生成')
})

it('已完成的测试从个人媒体地址播放，失败时显示原因', () => {
  const success = view({ ...previous, status: 'succeeded', video_object_id: 'result-id' })
  expect(success).toContain('src="/api/v1/media/result-id"')
  expect(success).toContain('controls=""')
  const failure = view({ ...previous, status: 'failed', error: '厂商拒绝本次生成' })
  expect(failure).toContain('厂商拒绝本次生成')
  expect(failure).not.toContain('<video')
})

it('原订阅仍报告等待时，重启后的数据库终态能恢复同一个结果并停止查询', async () => {
  const { watchVideoProbe } = await import('./watchVideoProbe')
  const { taskApi } = await import('../../api/tasks')
  vi.useFakeTimers()
  const visibility = Object.assign(new EventTarget(), { visibilityState: 'visible' as const })
  const success: Task = { ...previous, status: 'succeeded', video_object_id: 'saved' }
  const read = vi.spyOn(taskApi, 'get').mockRejectedValueOnce(new Error('后端重启中')).mockResolvedValueOnce(success)
  const finished = vi.fn()
  const unavailable = vi.fn()
  const cleanup = watchVideoProbe(previous, visibility, finished, unavailable)
  try {
    await vi.advanceTimersByTimeAsync(3000)
    expect(finished).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(6000)
    expect(read).toHaveBeenCalledTimes(2)
    expect(read).toHaveBeenCalledWith(previous, expect.any(AbortSignal))
    expect(finished).toHaveBeenCalledWith(success)
    expect(unavailable).not.toHaveBeenCalled()
  } finally {
    cleanup()
    vi.restoreAllMocks()
    vi.useRealTimers()
  }
})
