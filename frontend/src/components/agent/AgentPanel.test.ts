import { App } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { AgentConversation } from '../../api/types'
import AgentPanel from './AgentPanel'

const state = vi.hoisted(() => ({ conversations: [] as unknown[] | undefined, canUseAgent: true }))
vi.mock('../../hooks/useLoader', () => ({ useLoader: () => ({ data: state.conversations, loading: false, error: null, reload: () => {} }) }))
vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => ({ canUseAgent: state.canUseAgent, canUseAgentNow: () => state.canUseAgent }) }))
vi.mock('./AgentConversationView', () => ({ default: ({ cid }: { cid: number }) => createElement('div', { 'data-conversation': cid }) }))

const conversation = (over: Partial<AgentConversation> = {}): AgentConversation => ({
  id: 1, user_id: 3, project_id: 9, title: '主会话', auto_execute: false, summary: '', summarized_until: null,
  history_sealed: false, last_message_at: null, mcp_plugin_ids: [], writable: true, created_at: '', updated_at: '', ...over,
})
const render = () => renderToString(createElement(App, null, createElement(StaticRouter, { location: '/projects/9/overview' },
  createElement(AgentPanel, { projectId: 9 })))).replaceAll('<!-- -->', '')
const button = (html: string, label: string) => new RegExp(`<button[^>]*aria-label="${label}"[^>]*>`).exec(html)?.[0] ?? ''

describe('智能体会话列表的协作者状态', () => {
  it('作者 / 可写编辑者：可新建、改名、删除可写会话', () => {
    state.canUseAgent = true
    state.conversations = [conversation()]
    const html = render()
    for (const label of ['新建会话', '重命名会话', '删除会话']) expect(button(html, label)).not.toContain('disabled=""')
  })

  it('重邀者的旧会话（writable=false）只读：不能改名或删除，仍可新建本轮会话', () => {
    state.canUseAgent = true
    state.conversations = [conversation({ writable: false })]
    const html = render()
    expect(html).toContain('主会话（只读）')
    expect(button(html, '新建会话')).not.toContain('disabled=""')
    expect(button(html, '重命名会话')).toContain('disabled=""')
    expect(button(html, '删除会话')).toContain('disabled=""')
  })

  it('查看者：只读本人会话，不能新建；无会话时说明而不提供开始按钮', () => {
    state.canUseAgent = false
    state.conversations = [conversation({ writable: false })]
    let html = render()
    expect(button(html, '新建会话')).toContain('disabled=""')
    expect(button(html, '重命名会话')).toContain('disabled=""')
    expect(html).toContain('data-conversation="1"')
    state.conversations = []
    html = render()
    expect(html).toContain('当前为查看者，只能查看本人已有的智能体会话')
    expect(html).not.toContain('开始新会话')
  })
})
