import { DeleteOutlined, DownloadOutlined, EditOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Empty, Input, List, Modal, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { downloadJson } from '../api/cards'
import { errorMessage } from '../api/client'
import { COMPONENT_LIMITS, MAX_COMPONENT_IMPORT_BYTES, promptComponentApi } from '../api/promptComponents'
import type { PromptComponentBrief, PromptComponentImportResult, PromptComponentUsage } from '../api/types'
import JsonImportButton from '../components/JsonImportButton'
import ComponentEditDrawer from '../components/promptComponents/ComponentEditDrawer'
import { ComponentFailureList, ComponentUsageList } from '../components/promptComponents/ComponentUsageList'
import { useLoader } from '../hooks/useLoader'
import { datedFilename } from '../utils/jsonFile'
import { COMPONENT_IMPORT_LABELS, componentRef, normalizeFailures, readUsages, summarizeComponentImport } from '../utils/promptComponents'

const IMPORT_COLORS = { created: 'green', updated: 'blue', unchanged: 'default', invalid: 'red' } as const

/** 提示词组件库：本人的可复用片段；列表（引用数）、搜索、新建 / 编辑抽屉（使用关系）、删除（被引用时列出引用方）、导入导出。 */
export default function PromptComponentsPage() {
  const { message, modal } = App.useApp()
  const [searchParams, setSearchParams] = useSearchParams()
  const [q, setQ] = useState('')
  const loader = useCallback(() => promptComponentApi.list(q), [q])
  const { data: items = [], loading, error, reload } = useLoader(loader)
  // 编辑中的组件：URL ?id= 打开已有组件（引用方标签跳转过来），'new' 为新建
  const idParam = searchParams.get('id')
  const editingId = idParam && /^\d+$/.test(idParam) ? Number(idParam) : null
  const [creating, setCreating] = useState(false)
  const [importing, setImporting] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [importResult, setImportResult] = useState<PromptComponentImportResult | null>(null)
  const [blocked, setBlocked] = useState<{ title: string; usages: PromptComponentUsage[] } | null>(null)

  const openEdit = (id: number) => {
    setCreating(false)
    setSearchParams({ id: String(id) }, { replace: true })
  }
  const closeDrawer = () => {
    setCreating(false)
    if (idParam) setSearchParams({}, { replace: true })
  }

  const doDelete = async (c: PromptComponentBrief) => {
    try {
      await promptComponentApi.remove(c.id)
      message.success('已删除')
      reload()
    } catch (err) {
      const usages = readUsages(err)
      if (usages) setBlocked({ title: `组件「${c.name}」正被引用，不能删除`, usages })
      else message.error(errorMessage(err))
    }
  }

  const confirmDelete = (c: PromptComponentBrief) => {
    if (c.usage_count > 0) {
      // 列表里已知被引用：直接取详情展示引用方，不发删除请求
      void promptComponentApi.get(c.id).then(
        (detail) => setBlocked({ title: `组件「${c.name}」正被引用，不能删除`, usages: detail.usages }),
        (err: unknown) => message.error(errorMessage(err)),
      )
      return
    }
    modal.confirm({
      title: `删除组件「${c.name}」？`,
      content: '删除后无法恢复。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => doDelete(c),
    })
  }

  const handleExport = async () => {
    setExporting(true)
    try {
      const data = await promptComponentApi.export()
      const count = Array.isArray(data.items) ? data.items.length : 0
      if (!count) {
        message.info('还没有组件，没有可导出的内容')
        return
      }
      downloadJson(data, datedFilename('ainovel-提示词组件'))
      message.success(`已导出 ${count} 个组件`)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setExporting(false)
    }
  }

  const handleImport = async (data: unknown) => {
    setImporting(true)
    try {
      setImportResult(await promptComponentApi.import(data))
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setImporting(false)
    }
  }

  return (
    <div style={{ padding: 24, maxWidth: 1200, margin: '0 auto' }}>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }} wrap>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            提示词组件
          </Typography.Title>
          <Typography.Text type="secondary">
            把反复出现的提示词片段保存为组件，在自己的 Skill 正文和模板覆盖中写 <Typography.Text code>{componentRef('标识')}</Typography.Text> 引用；修改组件后所有引用方同时生效。最多 {COMPONENT_LIMITS.count} 个。
          </Typography.Text>
        </div>
        <Space wrap>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => { setSearchParams({}, { replace: true }); setCreating(true) }}>
            新建组件
          </Button>
          <Button icon={<ReloadOutlined />} onClick={reload}>
            刷新
          </Button>
          <JsonImportButton maxBytes={MAX_COMPONENT_IMPORT_BYTES} tooLarge="组件文件过大，上限为 1MB" loading={importing} onData={handleImport}>
            导入
          </JsonImportButton>
          <Button icon={<DownloadOutlined />} loading={exporting} onClick={() => void handleExport()}>
            导出
          </Button>
        </Space>
      </Space>
      <Input.Search
        aria-label="搜索组件"
        allowClear
        placeholder="按标识、名称、说明或内容搜索"
        maxLength={100}
        onSearch={(v) => setQ(v.trim())}
        style={{ marginBottom: 16, maxWidth: 420 }}
      />
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      <Card size="small">
        {loading && items.length === 0 ? (
          <Spin style={{ display: 'block', margin: 40 }} />
        ) : items.length === 0 ? (
          <Empty description={q ? '没有匹配的组件' : '还没有组件'} />
        ) : (
          <List<PromptComponentBrief>
            aria-label="组件列表"
            dataSource={items}
            renderItem={(c) => (
              <List.Item
                data-component-id={c.id}
                style={{ flexWrap: 'wrap', gap: 8 }}
                actions={[
                  <Button key="edit" type="link" size="small" icon={<EditOutlined />} onClick={() => openEdit(c.id)}>
                    编辑
                  </Button>,
                  <Button key="delete" type="link" size="small" danger icon={<DeleteOutlined />} onClick={() => confirmDelete(c)}>
                    删除
                  </Button>,
                ]}
              >
                <List.Item.Meta
                  title={
                    <Space size={6} wrap>
                      <Typography.Link onClick={() => openEdit(c.id)}>{c.name}</Typography.Link>
                      <Typography.Text code style={{ overflowWrap: 'anywhere' }}>{c.slug}</Typography.Text>
                      <Tag color={c.usage_count ? 'geekblue' : 'default'}>引用 {c.usage_count}</Tag>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {c.content_chars} 字
                      </Typography.Text>
                    </Space>
                  }
                  description={<span style={{ overflowWrap: 'anywhere' }}>{c.description || '暂无说明'}</span>}
                />
              </List.Item>
            )}
          />
        )}
      </Card>
      <ComponentEditDrawer
        open={creating || editingId !== null}
        componentId={creating ? null : editingId}
        onClose={closeDrawer}
        onSaved={(saved) => {
          reload()
          if (creating) openEdit(saved.id)
        }}
      />
      <Modal open={blocked !== null} title={blocked?.title} footer={null} onCancel={() => setBlocked(null)}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text type="secondary">请先在以下位置移除对它的引用：</Typography.Text>
          <ComponentUsageList usages={blocked?.usages ?? []} />
        </Space>
      </Modal>
      <Modal open={importResult !== null} title="导入结果" footer={null} onCancel={() => setImportResult(null)}>
        {importResult && (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text>{summarizeComponentImport(importResult)}</Typography.Text>
            <ul aria-label="导入逐项结果" style={{ margin: 0, paddingLeft: 18 }}>
              {importResult.items.map((i, idx) => (
                <li key={idx} style={{ overflowWrap: 'anywhere' }}>
                  <Space size={4} wrap>
                    <Tag color={IMPORT_COLORS[i.status]}>{COMPONENT_IMPORT_LABELS[i.status]}</Tag>
                    <Typography.Text code>{i.slug || '（无标识）'}</Typography.Text>
                    {i.failures.length > 0 ? (
                      <Typography.Text type="danger">修改会让引用它的模板覆盖校验失败，未更新：</Typography.Text>
                    ) : (
                      i.message && <Typography.Text type="danger">{i.message}</Typography.Text>
                    )}
                  </Space>
                  {i.failures.length > 0 && <ComponentFailureList failures={normalizeFailures(i.failures)} />}
                </li>
              ))}
            </ul>
          </Space>
        )}
      </Modal>
    </div>
  )
}
