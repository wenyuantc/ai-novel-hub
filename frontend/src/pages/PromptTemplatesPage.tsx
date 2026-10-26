import { DownloadOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Col, Empty, Menu, Row, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { downloadJson } from '../api/cards'
import { errorMessage } from '../api/client'
import { MAX_PROMPT_IMPORT_BYTES, promptApi } from '../api/prompts'
import type { PromptImportResult } from '../api/types'
import JsonImportButton from '../components/JsonImportButton'
import PromptEditor from '../components/prompts/PromptEditor'
import PromptImportResultModal from '../components/prompts/PromptImportResultModal'
import { useLoader } from '../hooks/useLoader'
import { datedFilename } from '../utils/jsonFile'
import { groupTemplates, templateBadges } from '../utils/prompts'

/** 提示词模板：按分类列出全部模板（已覆盖 / 已停用 / 默认已更新），右侧编辑当前选中的模板；支持导入导出我的覆盖。 */
export default function PromptTemplatesPage() {
  const { message, modal } = App.useApp()
  const [searchParams, setSearchParams] = useSearchParams()
  const loader = useCallback(() => promptApi.list(), [])
  const { data: list = [], loading, error, reload } = useLoader(loader)
  const groups = useMemo(() => groupTemplates(list), [list])
  const names = useMemo(() => Object.fromEntries(list.map((t) => [t.key, t.name])), [list])
  const selected = searchParams.get('key') ?? list[0]?.key ?? null
  const dirtyRef = useRef(false)
  const [importing, setImporting] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [importResult, setImportResult] = useState<PromptImportResult | null>(null)

  const onDirtyChange = useCallback((d: boolean) => {
    dirtyRef.current = d
  }, [])

  const select = (key: string) => {
    if (key === selected) return
    const go = () => setSearchParams({ key }, { replace: true })
    if (!dirtyRef.current) return go()
    modal.confirm({
      title: '放弃未保存的修改？',
      content: '当前模板有未保存的修改，切换后会丢失。',
      okText: '放弃并切换',
      cancelText: '继续编辑',
      onOk: go,
    })
  }

  const handleExport = async () => {
    setExporting(true)
    try {
      const data = await promptApi.export()
      const count = Array.isArray(data.items) ? data.items.length : 0
      downloadJson(data, datedFilename('ainovel-提示词模板'))
      message.success(count ? `已导出 ${count} 个覆盖` : '还没有覆盖任何模板，已导出空文件')
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setExporting(false)
    }
  }

  const handleImport = async (data: unknown) => {
    setImporting(true)
    try {
      setImportResult(await promptApi.import(data))
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setImporting(false)
    }
  }

  return (
    <div style={{ padding: 24, maxWidth: 1600, margin: '0 auto' }}>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }} wrap>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            提示词模板
          </Typography.Title>
          <Typography.Text type="secondary">
            每个 AI 功能一个模板。修改并保存后成为我的覆盖，对我的全部项目生效；JSON 输出结构等锁定内容由系统提供，只能调整位置。
          </Typography.Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={reload}>
            刷新
          </Button>
          <JsonImportButton
            maxBytes={MAX_PROMPT_IMPORT_BYTES}
            tooLarge="模板文件过大，上限为 1MB"
            loading={importing}
            onData={handleImport}
          >
            导入
          </JsonImportButton>
          <Button icon={<DownloadOutlined />} loading={exporting} onClick={() => void handleExport()}>
            导出我的覆盖
          </Button>
        </Space>
      </Space>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      <Row gutter={16}>
        <Col xs={24} lg={6}>
          <Card size="small" styles={{ body: { padding: 0 } }}>
            {loading && list.length === 0 ? (
              <Spin style={{ display: 'block', margin: 40 }} />
            ) : list.length === 0 ? (
              <Empty style={{ margin: 24 }} />
            ) : (
              <Menu
                mode="inline"
                selectedKeys={selected ? [selected] : []}
                onClick={({ key }) => select(key)}
                style={{ borderInlineEnd: 'none' }}
                items={groups.map((g) => ({
                  type: 'group' as const,
                  key: `g-${g.category}`,
                  label: g.label,
                  children: g.items.map((t) => ({
                    key: t.key,
                    label: (
                      <Space size={4}>
                        <span>{t.name}</span>
                        {templateBadges(t).map((b) => (
                          <Tag key={b.label} color={b.color} style={{ marginInlineEnd: 0 }}>
                            {b.label}
                          </Tag>
                        ))}
                      </Space>
                    ),
                  })),
                }))}
              />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={18}>
          {selected ? (
            <PromptEditor key={selected} templateKey={selected} onChanged={reload} onDirtyChange={onDirtyChange} />
          ) : (
            !loading && <Empty description="请选择一个模板" />
          )}
        </Col>
      </Row>
      <PromptImportResultModal result={importResult} names={names} onClose={() => setImportResult(null)} />
    </div>
  )
}
