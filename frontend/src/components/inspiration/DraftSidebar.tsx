import { DeleteOutlined } from '@ant-design/icons'
import { App, Button, Card, Input, List, Space, Tag, Typography } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { inspirationApi } from '../../api/inspiration'
import type { InspirationCurrent, InspirationSession } from '../../api/types'
import { draftSummary, MAX_NOTES, NOTE_MAX, scaleText, STEP_DISPLAY } from '../../utils/inspiration'

interface Props {
  session: InspirationSession
  onSession: (s: InspirationSession) => void
  onGoTo: (key: InspirationCurrent) => void
  disabled?: boolean
}

/** 右侧「立项草稿」：汇总已定的值（点击跳到对应步骤）+ 补充想法列表与添加框（补充想法不触发「可能需要更新」）。 */
export default function DraftSidebar({ session, onSession, onGoTo, disabled }: Props) {
  const { message } = App.useApp()
  const state = session.state
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const full = state.notes.length >= MAX_NOTES

  const run = async (fn: () => Promise<InspirationSession>, ok?: () => void) => {
    setBusy(true)
    try {
      onSession(await fn())
      ok?.()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const addNote = () => {
    const text = note.trim()
    if (!text) return
    void run(
      () => inspirationApi.addNote(session.id, text),
      () => setNote(''),
    )
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card size="small" title="立项草稿">
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {state.genre && (
            <div>
              <Typography.Text type="secondary">题材：</Typography.Text>
              {state.genre}
            </div>
          )}
          {draftSummary(state).map((it) => (
            <div key={it.key}>
              <Space size={4}>
                <Typography.Link onClick={() => onGoTo(it.key)} disabled={disabled}>
                  {it.label}
                </Typography.Link>
                <Tag color={STEP_DISPLAY[it.status].color} style={{ marginInlineEnd: 0 }}>
                  {STEP_DISPLAY[it.status].label}
                </Tag>
              </Space>
              {it.text && (
                <Typography.Paragraph ellipsis={{ rows: 3, expandable: true }} style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>
                  {it.text}
                </Typography.Paragraph>
              )}
            </div>
          ))}
          <div>
            <Typography.Link onClick={() => onGoTo('scale')} disabled={disabled}>
              规模
            </Typography.Link>
            ：{state.scale ? scaleText(state.scale) : <Typography.Text type="secondary">未设置</Typography.Text>}
            {state.scale?.target_words ? `；目标 ${state.scale.target_words.toLocaleString('zh-CN')} 字` : ''}
          </div>
        </Space>
      </Card>
      <Card size="small" title={`补充想法（${state.notes.length}/${MAX_NOTES}）`}>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          随时补一句，之后生成选项时都会带上；不会让已定的步骤变成「可能需要更新」。
        </Typography.Paragraph>
        {state.notes.length > 0 && (
          <List
            size="small"
            dataSource={state.notes.map((text, index) => ({ text, index }))}
            renderItem={({ text, index }) => (
              <List.Item
                actions={[
                  <Button
                    key="del"
                    type="text"
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    disabled={busy}
                    onClick={() => void run(() => inspirationApi.deleteNote(session.id, index))}
                  />,
                ]}
              >
                <span style={{ whiteSpace: 'pre-wrap' }}>{text}</span>
              </List.Item>
            )}
            style={{ marginBottom: 8 }}
          />
        )}
        <Space.Compact style={{ width: '100%' }}>
          <Input
            value={note}
            maxLength={NOTE_MAX}
            disabled={full}
            placeholder={full ? `最多 ${MAX_NOTES} 条` : '例如：主角最好是女性'}
            onChange={(e) => setNote(e.target.value)}
            onPressEnter={addNote}
          />
          <Button type="primary" loading={busy} disabled={full || !note.trim()} onClick={addNote}>
            添加
          </Button>
        </Space.Compact>
      </Card>
    </Space>
  )
}
