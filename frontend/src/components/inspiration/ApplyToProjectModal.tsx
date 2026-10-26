import { Alert, App, Checkbox, Empty, Form, Modal, Select, Spin, Table, Tag, Typography } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { errorMessage } from '../../api/client'
import { inspirationApi } from '../../api/inspiration'
import { projectApi } from '../../api/projects'
import type { InspirationApplyField, InspirationApplyPreviewItem } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { applyUnchanged, applyValueText, defaultApplyFields } from '../../utils/inspiration'

interface Props {
  sessionId: number
  /** 默认选中的项目（会话来源项目或已转为的项目） */
  defaultProjectId: number | null
  open: boolean
  onClose: () => void
}

/** 应用到已有项目：选项目 → 逐字段对比（当前 / 会话）→ 勾选（默认只勾项目里为空的字段）→ 应用。会话状态不变，可反复应用。 */
export default function ApplyToProjectModal({ open, ...rest }: Props) {
  if (!open) return null
  return <ApplyInner {...rest} />
}

function ApplyInner({ sessionId, defaultProjectId, onClose }: Omit<Props, 'open'>) {
  const { message } = App.useApp()
  const projectsLoader = useCallback(() => projectApi.list(), [])
  const { data: projects = [], loading: projectsLoading } = useLoader(projectsLoader)
  const [projectId, setProjectId] = useState<number | null>(defaultProjectId)
  const [items, setItems] = useState<InspirationApplyPreviewItem[] | null>(null)
  const [checked, setChecked] = useState<InspirationApplyField[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [applying, setApplying] = useState(false)

  useEffect(() => {
    if (projectId === null) return
    let alive = true
    const load = async () => {
      setLoading(true)
      setError(null)
      try {
        const list = await inspirationApi.applyPreview(sessionId, projectId)
        if (!alive) return
        setItems(list)
        setChecked(defaultApplyFields(list))
      } catch (err) {
        if (alive) {
          setItems(null)
          setError(errorMessage(err))
        }
      } finally {
        if (alive) setLoading(false)
      }
    }
    void load()
    return () => {
      alive = false
    }
  }, [sessionId, projectId])

  const project = projects.find((p) => p.id === projectId)

  const handleApply = async () => {
    if (projectId === null || checked.length === 0) return
    setApplying(true)
    try {
      const p = await inspirationApi.apply(sessionId, { project_id: projectId, fields: checked })
      message.success(`已应用 ${checked.length} 个字段到《${p.title}》`)
      onClose()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setApplying(false)
    }
  }

  return (
    <Modal
      open
      title="应用到已有项目"
      width={860}
      onCancel={onClose}
      onOk={() => void handleApply()}
      okText={`应用勾选的 ${checked.length} 项`}
      okButtonProps={{ disabled: checked.length === 0 || loading }}
      confirmLoading={applying}
      cancelText="取消"
    >
      <Form layout="vertical">
        <Form.Item label="目标项目">
          <Select<number>
            showSearch
            loading={projectsLoading}
            value={projectId ?? undefined}
            onChange={setProjectId}
            placeholder="选择要更新的项目"
            optionFilterProp="label"
            options={projects.map((p) => ({ value: p.id, label: p.title }))}
          />
        </Form.Item>
      </Form>
      {project && (
        <Typography.Paragraph type="secondary">
          默认只勾选《{project.title}》中为空的字段；勾选已有内容的字段会覆盖原值。
          <Link to={`/projects/${project.id}/overview`} target="_blank">
            打开项目概览
          </Link>
        </Typography.Paragraph>
      )}
      {error && <Alert type="error" showIcon message={error} />}
      <Spin spinning={loading}>
        {projectId === null ? (
          <Empty description="先选择一个项目" />
        ) : items && items.length === 0 ? (
          <Empty description="会话里还没有可以应用的内容" />
        ) : items ? (
          <Table<InspirationApplyPreviewItem>
            rowKey="field"
            size="small"
            pagination={false}
            dataSource={items}
            columns={[
              {
                title: '',
                width: 40,
                render: (_, it) => (
                  <Checkbox
                    checked={checked.includes(it.field)}
                    disabled={applyUnchanged(it)}
                    onChange={(e) =>
                      setChecked((prev) =>
                        e.target.checked ? [...prev, it.field] : prev.filter((f) => f !== it.field),
                      )
                    }
                  />
                ),
              },
              {
                title: '字段',
                width: 110,
                render: (_, it) => (
                  <span>
                    {it.label}
                    {applyUnchanged(it) && (
                      <Tag style={{ marginLeft: 4 }} color="default">
                        相同
                      </Tag>
                    )}
                  </span>
                ),
              },
              {
                title: '项目当前',
                render: (_, it) => (
                  <Typography.Paragraph
                    type={it.default_checked ? 'secondary' : undefined}
                    ellipsis={{ rows: 4, expandable: true }}
                    style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}
                  >
                    {applyValueText(it.field, it.current)}
                  </Typography.Paragraph>
                ),
              },
              {
                title: '灵感会话',
                render: (_, it) => (
                  <Typography.Paragraph
                    ellipsis={{ rows: 4, expandable: true }}
                    style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}
                  >
                    {applyValueText(it.field, it.proposed)}
                  </Typography.Paragraph>
                ),
              },
            ]}
          />
        ) : null}
      </Spin>
    </Modal>
  )
}
