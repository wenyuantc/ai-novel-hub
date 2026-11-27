import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Col, Empty, Popconfirm, Row, Space, Spin, Typography } from 'antd'
import { ArrowDownOutlined, ArrowUpOutlined, HolderOutlined } from '@ant-design/icons'
import type { DramaAssets } from '../assetTypes'
import { dramaAssetApi } from '../assetApi'
import { moveShot, shotApi, type Shot } from '../shots'
import ShotInspector from './ShotInspector'

interface Props {
  pid: number
  sid: number
  eid: number
  canEditNow: () => boolean
  canEdit: boolean
  episodeRevision: number
  reloadKey: number
  onChange: () => void
  onTask?: (kind: 'drama.storyboard' | 'drama.prompts', params: Record<string, unknown>) => void
}

export default function ShotStep(props: Props) {
  return <EpisodeShots key={props.pid + ':' + props.sid + ':' + props.eid} {...props} />
}

function EpisodeShots({ sid, eid, canEdit, canEditNow, episodeRevision, reloadKey, onChange, onTask }: Props) {
  const [shots, setShots] = useState<Shot[]>([])
  const [assets, setAssets] = useState<DramaAssets | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [editing, setEditing] = useState<Shot | null>(null)
  const dirty = editing !== null
  const [busy, setBusy] = useState(false)
  const [loadedKey, setLoadedKey] = useState('')
  const [error, setError] = useState('')
  const [sequence, refresh] = useState(0)
  const [announcement, setAnnouncement] = useState('')
  const dragging = useRef<number | null>(null)
  const [over, setOver] = useState<number | null>(null)
  const active = useRef(true)
  const locked = useRef(false)
  const generation = useRef(0)
  const current = shots.find((s) => s.id === (editing?.id ?? selected)) ?? editing ?? shots[0]
  const ids = shots.map((s) => s.id)
  const position = current ? ids.indexOf(current.id) : -1
  const requestedKey = reloadKey + ':' + sequence
  const loading = requestedKey !== loadedKey
  const blocked = busy || dirty || loading

  useEffect(() => {
    active.current = true
    return () => {
      active.current = false
    }
  }, [])
  useEffect(() => {
    let relevant = true
    const version = ++generation.current
    Promise.all([shotApi.list(eid), dramaAssetApi.series(sid)])
      .then(([list, library]) => {
        if (!relevant || generation.current !== version) return
        setShots(list)
        setAssets(library)
        setError('')
      })
      .catch((err) => {
        if (relevant && generation.current === version) setError(err instanceof Error ? err.message : '读取分镜失败')
      })
      .finally(() => {
        if (relevant && generation.current === version) setLoadedKey(requestedKey)
      })
    return () => {
      relevant = false
    }
  }, [eid, sid, requestedKey])

  async function mutate(run: () => Promise<() => void>) {
    if (!canEditNow() || !canEdit || blocked || locked.current) return
    locked.current = true
    setBusy(true)
    setError('')
    try {
      const finish = await run()
      if (!active.current || !canEditNow()) return
      generation.current++
      finish()
      onChange()
    } catch (err) {
      if (active.current && canEditNow()) {
        setError(err instanceof Error ? err.message : '操作失败')
      }
    } finally {
      locked.current = false
      if (active.current) setBusy(false)
    }
  }

  function reorder(source: number, target: number) {
    const order = moveShot(ids, source, target)
    if (order === ids) return
    void mutate(async () => {
      const list = await shotApi.order(eid, episodeRevision, order)
      return () => {
        setShots(list)
        setSelected(source)
        setAnnouncement('分镜已移到第 ' + (order.indexOf(source) + 1) + ' 位')
      }
    })
  }

  const add = () =>
    void mutate(async () => {
      const row = await shotApi.create(eid, {})
      return () => {
        setShots((previous) => [...previous, row])
        setSelected(row.id)
      }
    })
  const remove = (row: Shot) =>
    void mutate(async () => {
      await shotApi.remove(row.id, row.revision)
      return () => {
        setShots((previous) => previous.filter((s) => s.id !== row.id))
        setSelected(null)
      }
    })

  if (loading && !assets) return <Spin />
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          action={
            <Button
              onClick={() => {
                refresh((n) => n + 1)
                onChange()
              }}
            >
              刷新
            </Button>
          }
        />
      )}
      {assets && (
        <>
          {canEdit && (
            <Space wrap>
              <Button onClick={add} disabled={blocked || shots.length >= 200}>
                新增分镜
              </Button>
              {onTask &&
                (shots.length > 0 ? (
                  <Popconfirm
                    title="重新拆分将替换本集的全部分镜，继续吗？"
                    okText="重新拆分"
                    cancelText="取消"
                    disabled={blocked}
                    onConfirm={() => onTask('drama.storyboard', {})}
                  >
                    <Button disabled={blocked}>重新拆分镜</Button>
                  </Popconfirm>
                ) : (
                  <Button disabled={blocked} onClick={() => onTask('drama.storyboard', {})}>
                    AI 拆分镜
                  </Button>
                ))}
            </Space>
          )}
          {!current ? (
            <Empty description="本集还没有分镜" />
          ) : (
            <Row gutter={[16, 16]}>
              <Col xs={24} lg={8}>
                <Card title="分镜列表" size="small">
                  <ol
                    aria-label="分镜顺序"
                    style={{ margin: 0, padding: 0, listStyle: 'none', maxHeight: '60vh', overflowY: 'auto' }}
                  >
                    {shots.map((row, index) => (
                      <li
                        key={row.id}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          marginBlock: 4,
                          outline: over === row.id ? '2px solid var(--ant-color-primary, Highlight)' : undefined,
                        }}
                        onDragOver={(event) => {
                          if (dragging.current === null || blocked || !canEdit) return
                          event.preventDefault()
                          event.dataTransfer.dropEffect = 'move'
                          setOver(row.id)
                        }}
                        onDrop={(event) => {
                          event.preventDefault()
                          if (dragging.current !== null) reorder(dragging.current, row.id)
                          dragging.current = null
                          setOver(null)
                        }}
                      >
                        {canEdit && (
                          <span
                            draggable={!blocked}
                            title="拖动排序"
                            aria-hidden="true"
                            style={{ padding: 6, cursor: 'grab' }}
                            onDragStart={(event) => {
                              dragging.current = row.id
                              event.dataTransfer.effectAllowed = 'move'
                              event.dataTransfer.setData('text/plain', String(row.id))
                            }}
                            onDragEnd={() => {
                              dragging.current = null
                              setOver(null)
                            }}
                          >
                            <HolderOutlined />
                          </span>
                        )}
                        <Button
                          block
                          type={current.id === row.id ? 'primary' : 'text'}
                          aria-pressed={current.id === row.id}
                          disabled={dirty && current.id !== row.id}
                          onClick={() => setSelected(row.id)}
                          style={{ minWidth: 0, justifyContent: 'start' }}
                        >
                          <Typography.Text ellipsis style={{ color: 'inherit' }}>
                            {index + 1}. {row.title || '未命名分镜'}
                          </Typography.Text>
                        </Button>
                      </li>
                    ))}
                  </ol>
                  {canEdit && (
                    <Space wrap>
                      <Button
                        icon={<ArrowUpOutlined />}
                        disabled={blocked || position < 1}
                        onClick={() => reorder(current.id, ids[position - 1])}
                      >
                        上移
                      </Button>
                      <Button
                        icon={<ArrowDownOutlined />}
                        disabled={blocked || position >= shots.length - 1}
                        onClick={() => reorder(current.id, ids[position + 1])}
                      >
                        下移
                      </Button>
                      <Popconfirm
                        title="删除当前分镜？"
                        okText="删除"
                        cancelText="取消"
                        disabled={blocked}
                        onConfirm={() => remove(current)}
                      >
                        <Button danger disabled={blocked}>
                          删除
                        </Button>
                      </Popconfirm>
                    </Space>
                  )}
                  <span role="status">{announcement}</span>
                </Card>
              </Col>
              <Col xs={24} lg={16}>
                <Card size="small" title={position < 0 ? '已删除分镜的草稿' : '分镜 ' + (position + 1)}>
                  <ShotInspector
                    key={current.id}
                    shot={current}
                    assets={assets}
                    canEdit={canEdit && !busy}
                    canEditNow={canEditNow}
                    onDirty={(value) => setEditing(value ? current : null)}
                    onSaved={(updated) => {
                      if (!active.current || !canEditNow()) return
                      setShots((previous) => previous.map((s) => (s.id === updated.id ? updated : s)))
                      onChange()
                    }}
                    onPrompt={
                      onTask && (() => onTask('drama.prompts', { targets: [{ type: 'shot', id: current.id }] }))
                    }
                  />
                </Card>
              </Col>
            </Row>
          )}
        </>
      )}
    </Space>
  )
}
