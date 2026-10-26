import {
  CopyOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  ExperimentOutlined,
  EyeOutlined,
  FileSearchOutlined,
  PlusOutlined,
} from '@ant-design/icons'
import { Alert, App, Button, Card, Col, Empty, Row, Space, Spin, Tag, Tooltip, Typography } from 'antd'
import { useMemo, useState } from 'react'
import { downloadJson } from '../api/cards'
import { errorMessage } from '../api/client'
import { MAX_STYLE_IMPORT_BYTES, styleApi } from '../api/styles'
import type { StyleDraft, WritingStyle } from '../api/types'
import JsonImportButton from '../components/JsonImportButton'
import StyleEditDrawer from '../components/styles/StyleEditDrawer'
import StyleExtractModal from '../components/styles/StyleExtractModal'
import type { TrialTarget } from '../components/styles/StyleTrialDrawer'
import StyleTrialDrawer from '../components/styles/StyleTrialDrawer'
import { useStyleStore, useStyles } from '../stores/styles'
import { datedFilename } from '../utils/jsonFile'
import { groupStyles, readStyleReferences, referencesText, STYLE_DIMENSIONS, summarizeStyleImport } from '../utils/styles'

/** 编辑抽屉状态：closed / 编辑已有 / 新建（可带提取草稿） */
type Editing = { style: WritingStyle | null; draft?: StyleDraft | null } | null

/** 写作风格库：预设（只读，可复制）与我的风格分开以卡片展示；新建 / 编辑 / 复制 / 删除（显示引用数）/ 导入导出 / 从样文提取 / 试写。 */
export default function StylesPage() {
  const { message, modal } = App.useApp()
  const { styles, loading, error } = useStyles()
  const reload = useStyleStore((s) => s.load)
  const { presets, mine } = useMemo(() => groupStyles(styles), [styles])
  const [editing, setEditing] = useState<Editing>(null)
  const [extractOpen, setExtractOpen] = useState(false)
  const [trial, setTrial] = useState<TrialTarget | null>(null)
  const [importing, setImporting] = useState(false)
  const [exporting, setExporting] = useState(false)

  const refresh = () => reload(true)

  const handleCopy = async (s: WritingStyle) => {
    try {
      const copied = await styleApi.copy(s.id)
      message.success(`已复制为「${copied.name}」`)
      await refresh()
      setEditing({ style: copied })
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  const doDelete = async (s: WritingStyle, force: boolean) => {
    try {
      await styleApi.remove(s.id, force)
      message.success('已删除')
      void refresh()
    } catch (err) {
      // 打开确认框之后又有新的引用：按最新引用数再确认一次
      const refs = readStyleReferences(err)
      if (refs && !force) confirmDelete(s, referencesText(refs))
      else message.error(errorMessage(err))
    }
  }

  const confirmDelete = (s: WritingStyle, refsText = referencesText(s.references)) => {
    modal.confirm({
      title: `删除风格「${s.name}」？`,
      content: refsText
        ? `该风格正被 ${refsText}使用。删除后，章节回落到项目默认风格，项目回落到不用风格。`
        : '删除后无法恢复。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => doDelete(s, refsText !== ''),
    })
  }

  const handleExport = async () => {
    setExporting(true)
    try {
      const data = await styleApi.export()
      const count = Array.isArray(data.items) ? data.items.length : 0
      if (!count) {
        message.info('还没有自己的风格，没有可导出的内容')
        return
      }
      downloadJson(data, datedFilename('ainovel-写作风格'))
      message.success(`已导出 ${count} 个风格`)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setExporting(false)
    }
  }

  const handleImport = async (data: unknown) => {
    setImporting(true)
    try {
      const r = await styleApi.import(data)
      void refresh()
      modal.info({
        title: '导入结果',
        content: (
          <Space direction="vertical">
            <Typography.Text>{summarizeStyleImport(r)}</Typography.Text>
            {r.items.length > 0 && (
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {r.items.map((i) => (
                  <li key={i.id}>
                    {i.name}
                    {i.renamed_from && <Typography.Text type="secondary">（原名「{i.renamed_from}」）</Typography.Text>}
                  </li>
                ))}
              </ul>
            )}
            {r.warnings.map((w, i) => (
              <Typography.Text key={i} type="warning">
                {w}
              </Typography.Text>
            ))}
          </Space>
        ),
      })
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setImporting(false)
    }
  }

  const card = (s: WritingStyle) => {
    const dims = STYLE_DIMENSIONS.filter((d) => s[d.key]).slice(0, 3)
    const refs = referencesText(s.references)
    const actions = s.is_preset
      ? [
          <Button key="view" type="link" size="small" icon={<EyeOutlined />} onClick={() => setEditing({ style: s })}>
            查看
          </Button>,
          <Button key="copy" type="link" size="small" icon={<CopyOutlined />} onClick={() => void handleCopy(s)}>
            复制
          </Button>,
          <Button
            key="trial"
            type="link"
            size="small"
            icon={<ExperimentOutlined />}
            onClick={() => setTrial({ kind: 'saved', id: s.id, name: s.name })}
          >
            试写
          </Button>,
        ]
      : [
          <Button key="edit" type="link" size="small" icon={<EditOutlined />} onClick={() => setEditing({ style: s })}>
            编辑
          </Button>,
          <Button key="copy" type="link" size="small" icon={<CopyOutlined />} onClick={() => void handleCopy(s)}>
            复制
          </Button>,
          <Button
            key="trial"
            type="link"
            size="small"
            icon={<ExperimentOutlined />}
            onClick={() => setTrial({ kind: 'saved', id: s.id, name: s.name })}
          >
            试写
          </Button>,
          <Button key="del" type="link" size="small" danger icon={<DeleteOutlined />} onClick={() => confirmDelete(s)}>
            删除
          </Button>,
        ]
    return (
      <Col xs={24} md={12} xl={8} key={s.id}>
        <Card
          size="small"
          title={
            <Space>
              {s.name}
              {s.is_preset && <Tag color="purple">预设</Tag>}
            </Space>
          }
          extra={
            refs && (
              <Tooltip title={`被 ${refs}使用`}>
                <Tag color="blue">使用中</Tag>
              </Tooltip>
            )
          }
          actions={actions}
          style={{ height: '100%' }}
        >
          <Typography.Paragraph type={s.description ? undefined : 'secondary'} ellipsis={{ rows: 2 }}>
            {s.description || '（没有一句话描述）'}
          </Typography.Paragraph>
          {dims.map((d) => (
            <Typography.Paragraph key={d.key} ellipsis={{ rows: 1 }} style={{ marginBottom: 4, fontSize: 12 }}>
              <Typography.Text type="secondary">{d.label}：</Typography.Text>
              {s[d.key]}
            </Typography.Paragraph>
          ))}
        </Card>
      </Col>
    )
  }

  return (
    <div style={{ padding: 24, maxWidth: 1400, margin: '0 auto' }}>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }} wrap>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            写作风格库
          </Typography.Title>
          <Typography.Text type="secondary">
            生成时按「临时选择 &gt; 章节风格 &gt; 项目默认 &gt; 不用风格」决定使用哪个风格。项目默认在项目概览设置，章节风格在章节编辑器设置。
          </Typography.Text>
        </div>
        <Space wrap>
          <Button icon={<FileSearchOutlined />} onClick={() => setExtractOpen(true)}>
            从样文提取
          </Button>
          <JsonImportButton
            maxBytes={MAX_STYLE_IMPORT_BYTES}
            tooLarge="风格文件过大，上限为 1MB"
            loading={importing}
            onData={handleImport}
          >
            导入
          </JsonImportButton>
          <Button icon={<DownloadOutlined />} loading={exporting} onClick={() => void handleExport()}>
            导出我的风格
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setEditing({ style: null })}>
            新建风格
          </Button>
        </Space>
      </Space>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      {loading && styles.length === 0 ? (
        <Spin style={{ display: 'block', margin: 60 }} />
      ) : (
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <div>
            <Typography.Title level={5}>我的风格（{mine.length}）</Typography.Title>
            {mine.length ? (
              <Row gutter={[16, 16]}>{mine.map(card)}</Row>
            ) : (
              <Empty description="还没有自己的风格：新建、从样文提取，或复制一个预设后修改" />
            )}
          </div>
          <div>
            <Typography.Title level={5}>预设风格（{presets.length}）</Typography.Title>
            <Row gutter={[16, 16]}>{presets.map(card)}</Row>
          </div>
        </Space>
      )}
      <StyleEditDrawer
        open={editing !== null}
        style={editing?.style ?? null}
        initialDraft={editing?.draft}
        onClose={() => setEditing(null)}
        onSaved={(s) => {
          void refresh()
          setEditing({ style: s })
        }}
        onCopy={(s) => void handleCopy(s)}
        onTrial={(draft) => setTrial({ kind: 'draft', draft })}
      />
      <StyleExtractModal
        open={extractOpen}
        onClose={() => setExtractOpen(false)}
        onSaved={(s) => {
          setExtractOpen(false)
          void refresh()
          setEditing({ style: s })
        }}
      />
      <StyleTrialDrawer target={trial} onClose={() => setTrial(null)} />
    </div>
  )
}
