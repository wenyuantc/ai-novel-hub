import { EditOutlined } from '@ant-design/icons'
import { Alert, App, Button, Collapse, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useMemo, useState } from 'react'
import type { BookChapterView, BookImport, BookOp, BookOpRequest } from '../../api/types'
import { useIsMobile } from '../../hooks/useIsMobile'
import {
  BOOK_OP_LABELS,
  BOOK_WARNING_LABELS,
  BOOK_WARNING_ORDER,
  filterChapters,
  filterOptions,
  formatChars,
  isVolumeStart,
  REGEX_MAX_LEN,
  totalWarnings,
  volumeLabel,
  type ChapterFilter,
} from '../../utils/bookImport'
import ChapterCard from './ChapterCard'
import ChapterOpsMenu, { type ChapterAction } from './ChapterOpsMenu'
import ChapterTextDrawer from './ChapterTextDrawer'

interface Props {
  record: BookImport
  busy: boolean
  /** 校正操作 */
  onOp: (body: Omit<BookOpRequest, 'revision'>, success?: string) => Promise<boolean>
  /** 重切（auto / regex） */
  onResplit: (mode: 'auto' | 'regex', pattern: string | null) => Promise<boolean>
}

const REGEX_EXAMPLES = ['^第[0-9一二三四五六七八九十百千零〇两]+章.*$', '^Chapter\\s*\\d+.*$', '^【\\d+】.*$']

/**
 * 第 2 步：校正切章结果。顶部 notices、提示汇总与筛选、自定义正则重切；主体是虚拟化的章节列表（3000 行，antd Table virtual），
 * 桌面为表格（标题可直接编辑），移动端为卡片（操作收进「⋮」菜单）；点击行打开带行号的全文抽屉，可从某一行拆分。
 * 每次操作的响应替换整个章节列表；修订号过期由页面重新拉取。
 */
export default function CorrectionStep({ record, busy, onOp, onResplit }: Props) {
  const { modal } = App.useApp()
  const isMobile = useIsMobile()
  const [filter, setFilter] = useState<ChapterFilter>('all')
  const [viewing, setViewing] = useState<BookChapterView | null>(null)
  const [titleOp, setTitleOp] = useState<{ op: BookOp; chapter: BookChapterView } | null>(null)
  const [titleForm] = Form.useForm<{ title: string }>()
  const [pattern, setPattern] = useState(record.split_pattern ?? '')

  const chapters = record.chapters
  const total = chapters.length
  // 卷首章（与上一章卷号不同）按原始位置判断，筛选后仍正确
  const volumeStarts = useMemo(() => {
    const set = new Set<number>()
    chapters.forEach((_, i) => {
      if (isVolumeStart(chapters, i)) set.add(chapters[i].index)
    })
    return set
  }, [chapters])
  const rows = useMemo(() => filterChapters(chapters, filter), [chapters, filter])
  const warningTotal = totalWarnings(record.warning_counts)

  const handleAction = (action: ChapterAction, ch: BookChapterView) => {
    if (action === 'view' || action === 'split') {
      setViewing(ch)
      return
    }
    if (action === 'rename' || action === 'start_volume' || action === 'rename_volume') {
      titleForm.setFieldsValue({ title: action === 'rename' ? ch.title : action === 'rename_volume' ? ch.volume_title : '' })
      setTitleOp({ op: action, chapter: ch })
      return
    }
    if (action === 'delete') {
      modal.confirm({
        title: `删除第 ${ch.index} 章「${ch.title}」？`,
        content: '这一章的内容不会被导入；可用「恢复自动切章」或重新上传找回。',
        okText: '删除',
        okButtonProps: { danger: true },
        cancelText: '取消',
        onOk: () => onOp({ op: 'delete', index: ch.index }, '已删除'),
      })
      return
    }
    void onOp({ op: action, index: ch.index }, action === 'merge_next' ? '已合并' : '已取消分卷')
  }

  const submitTitle = async () => {
    if (!titleOp) return
    const { title } = await titleForm.validateFields()
    const ok = await onOp({ op: titleOp.op, index: titleOp.chapter.index, title: title.trim() })
    if (ok) setTitleOp(null)
  }

  const desktopColumns: ColumnsType<BookChapterView> = [
    { title: '序号', dataIndex: 'index', width: 70 },
    {
      title: '分卷',
      key: 'volume',
      width: 150,
      render: (_, ch) => {
        const v = volumeLabel(ch)
        if (!v) return null
        return volumeStarts.has(ch.index) ? (
          <Tag color="blue" style={{ maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {v}
          </Tag>
        ) : (
          <Typography.Text type="secondary" ellipsis={{ tooltip: v }}>
            {v}
          </Typography.Text>
        )
      },
    },
    {
      title: '标题',
      key: 'title',
      // 用 CSS 省略而不是 Typography ellipsis + editable：后者在虚拟表格行重渲染时会把宽度量成 0，标题只剩「...」
      render: (_, ch) => (
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, minWidth: 0 }}>
          <span
            title={ch.title}
            style={{ flex: '0 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
          >
            {ch.title}
          </span>
          {!busy && (
            <Tooltip title="改标题">
              <Button
                type="link"
                size="small"
                icon={<EditOutlined />}
                aria-label={`改第 ${ch.index} 章标题`}
                // 阻止冒泡：改标题时不打开全文抽屉
                onClick={(e) => {
                  e.stopPropagation()
                  handleAction('rename', ch)
                }}
                style={{ flex: 'none', paddingInline: 0 }}
              />
            </Tooltip>
          )}
        </div>
      ),
    },
    { title: '字数', dataIndex: 'chars', width: 90, render: (n: number) => formatChars(n) },
    {
      title: '开头一行',
      dataIndex: 'first_line',
      render: (text: string) => (
        <Typography.Text type="secondary" ellipsis={{ tooltip: text }}>
          {text}
        </Typography.Text>
      ),
    },
    {
      title: '提示',
      key: 'warnings',
      width: 150,
      render: (_, ch) => (
        <Space size={2} wrap={false}>
          {ch.warnings.map((w) => (
            <Tooltip key={w} title={BOOK_WARNING_LABELS[w].hint}>
              <Tag color={BOOK_WARNING_LABELS[w].color} style={{ marginInlineEnd: 0 }}>
                {BOOK_WARNING_LABELS[w].label}
              </Tag>
            </Tooltip>
          ))}
        </Space>
      ),
    },
    {
      title: '操作',
      key: 'ops',
      width: 64,
      render: (_, ch) => <ChapterOpsMenu chapter={ch} total={total} disabled={busy} onAction={handleAction} />,
    },
  ]

  const mobileColumns: ColumnsType<BookChapterView> = [
    {
      key: 'card',
      render: (_, ch) => (
        <ChapterCard
          chapter={ch}
          total={total}
          volumeStart={volumeStarts.has(ch.index)}
          disabled={busy}
          onOpen={setViewing}
          onAction={handleAction}
        />
      ),
    },
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {record.notices.length > 0 && (
        <Alert
          type="info"
          showIcon
          message="解析说明"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {record.notices.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          }
        />
      )}
      {record.too_many && (
        <Alert
          type="warning"
          showIcon
          message={`共 ${record.chapter_count} 章，超过一次最多导入的 ${record.max_import_chapters} 章，下一步需要缩小导入范围。`}
        />
      )}
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 }}>
        <Typography.Text>
          共 <Typography.Text strong>{record.chapter_count}</Typography.Text> 章 · {formatChars(record.total_chars)} ·{' '}
          {record.split_mode === 'regex' ? '自定义正则切章' : '自动切章'}
        </Typography.Text>
        {warningTotal > 0 ? (
          <Space size={4} wrap>
            {BOOK_WARNING_ORDER.filter((k) => record.warning_counts[k] > 0).map((k) => (
              <Tooltip key={k} title={BOOK_WARNING_LABELS[k].hint}>
                <Tag color={BOOK_WARNING_LABELS[k].color} style={{ marginInlineEnd: 0, cursor: 'pointer' }} onClick={() => setFilter(k)}>
                  {BOOK_WARNING_LABELS[k].label} {record.warning_counts[k]}
                </Tag>
              </Tooltip>
            ))}
          </Space>
        ) : (
          <Tag color="success">没有需要注意的章节</Tag>
        )}
        <Select<ChapterFilter>
          value={filter}
          onChange={setFilter}
          options={filterOptions(record.warning_counts)}
          style={{ width: 160 }}
          aria-label="按提示筛选"
        />
      </div>
      <Collapse
        size="small"
        items={[
          {
            key: 'regex',
            label: '切得不对？用自定义正则重新切章',
            children: (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  正则只匹配不超过 60 字的行，匹配到的行作为章节标题；长度 ≤ {REGEX_MAX_LEN}，不能含嵌套量词（如 (a+)+）。
                  重切会清空当前的全部手工校正。示例：
                </Typography.Text>
                <Space wrap size={4}>
                  {REGEX_EXAMPLES.map((ex) => (
                    <Tag key={ex} style={{ cursor: 'pointer', marginInlineEnd: 0 }} onClick={() => setPattern(ex)}>
                      <code>{ex}</code>
                    </Tag>
                  ))}
                </Space>
                <Input
                  value={pattern}
                  onChange={(e) => setPattern(e.target.value)}
                  maxLength={REGEX_MAX_LEN}
                  placeholder="例如：^第\d+章.*$"
                  style={{ fontFamily: 'monospace' }}
                  aria-label="切章正则"
                />
                <Space wrap>
                  <Popconfirm
                    title="按正则重新切章？"
                    description="当前的手工校正（改标题、合并、拆分、分卷等）都会被清空。"
                    okText="重切"
                    cancelText="取消"
                    disabled={!pattern.trim() || busy}
                    onConfirm={() => onResplit('regex', pattern.trim())}
                  >
                    <Button type="primary" disabled={!pattern.trim() || busy}>
                      按正则重切
                    </Button>
                  </Popconfirm>
                  <Popconfirm
                    title="恢复自动切章？"
                    description="当前的手工校正都会被清空。"
                    okText="恢复"
                    cancelText="取消"
                    disabled={busy}
                    onConfirm={() => onResplit('auto', null)}
                  >
                    <Button disabled={busy}>恢复自动切章</Button>
                  </Popconfirm>
                </Space>
              </Space>
            ),
          },
        ]}
      />
      <Table<BookChapterView>
        rowKey="index"
        size="small"
        virtual
        scroll={{ y: isMobile ? 480 : 560 }}
        pagination={false}
        showHeader={!isMobile}
        columns={isMobile ? mobileColumns : desktopColumns}
        dataSource={rows}
        loading={busy}
        onRow={isMobile ? undefined : (ch) => ({ onClick: () => setViewing(ch), style: { cursor: 'pointer' } })}
        locale={{ emptyText: '没有符合筛选条件的章节' }}
      />
      <ChapterTextDrawer
        importId={record.id}
        chapter={viewing}
        revision={record.revision}
        disabled={busy}
        onClose={() => setViewing(null)}
        onSplit={(ch, line) => {
          setViewing(null)
          return onOp({ op: 'split', index: ch.index, line }, '已拆分')
        }}
      />
      <Modal
        open={titleOp !== null}
        title={titleOp ? `${BOOK_OP_LABELS[titleOp.op]} · 第 ${titleOp.chapter.index} 章` : ''}
        okText="确定"
        cancelText="取消"
        confirmLoading={busy}
        onOk={() => void submitTitle()}
        onCancel={() => setTitleOp(null)}
        destroyOnHidden
      >
        <Form form={titleForm} layout="vertical" preserve={false}>
          <Form.Item
            name="title"
            label={titleOp?.op === 'rename' ? '章节标题' : '卷名'}
            extra={titleOp?.op === 'start_volume' ? '从本章起到所在卷末尾的章节归入新卷' : undefined}
            rules={[
              { required: true, whitespace: true, message: '请填写名称' },
              { max: 200, message: '不超过 200 字' },
            ]}
          >
            <Input autoFocus onPressEnter={() => void submitTitle()} />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}
