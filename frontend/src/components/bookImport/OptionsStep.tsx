import { Alert, Card, Checkbox, Col, Descriptions, Form, Input, InputNumber, Row, Slider, Space, Spin, Typography } from 'antd'
import type { BookEstimate, BookImport, BookRange } from '../../api/types'
import AIConfigSelect from '../AIConfigSelect'
import {
  clampRange,
  DEEP_ANALYZE_DEFAULT_MAX,
  formatChars,
  rangeSize,
  withImportRange,
  type BookImportOptions,
} from '../../utils/bookImport'

interface Props {
  record: BookImport
  options: BookImportOptions
  onChange: (next: BookImportOptions) => void
  /** 用户是否手动改过「深度分析」（改过后导入范围变化不再重算默认值） */
  deepTouched: boolean
  onDeepTouched: () => void
  estimate: BookEstimate | null
  estimating: boolean
  estimateError: string | null
  /** 选项校验错误（null = 合法） */
  error: string | null
}

interface RangeFieldProps {
  label: string
  value: BookRange
  min: number
  max: number
  onChange: (r: BookRange) => void
}

/** 章节范围：滑块 + 起止输入框（输入值规整到 [min, max]，起 ≤ 止） */
function RangeField({ label, value, min, max, onChange }: RangeFieldProps) {
  const single = min >= max
  return (
    <div>
      <Space wrap size={4} style={{ marginBottom: 4 }}>
        <span>第</span>
        <InputNumber
          size="small"
          min={min}
          max={max}
          value={value[0]}
          aria-label={`${label}起始章`}
          onChange={(v) => typeof v === 'number' && onChange(clampRange([v, Math.max(v, value[1])], min, max))}
          style={{ width: 90 }}
        />
        <span>–</span>
        <InputNumber
          size="small"
          min={min}
          max={max}
          value={value[1]}
          aria-label={`${label}结束章`}
          onChange={(v) => typeof v === 'number' && onChange(clampRange([Math.min(v, value[0]), v], min, max))}
          style={{ width: 90 }}
        />
        <span>章，共 {rangeSize(value)} 章</span>
      </Space>
      {!single && (
        <Slider
          range
          min={min}
          max={max}
          value={value}
          onChange={(v) => onChange([v[0], v[1]])}
          tooltip={{ formatter: (v) => `第 ${v} 章` }}
        />
      )}
    </div>
  )
}

/** 第 3 步：书名 / 题材、导入范围与反推范围、提取风格与深度分析、AI 配置，以及预估的 AI 输入字数（防抖调用 estimate）。 */
export default function OptionsStep(props: Props) {
  const { record, options, onChange, deepTouched, onDeepTouched, estimate, estimating, estimateError, error } = props
  const count = record.chapter_count
  const importCount = rangeSize(options.importRange)
  return (
    <Row gutter={[24, 16]}>
      <Col xs={24} lg={14}>
        <Form layout="vertical">
          <Form.Item label="书名" required help="默认取 epub 元数据或文件名；导入后作为新项目的书名">
            <Input value={options.title} maxLength={200} onChange={(e) => onChange({ ...options, title: e.target.value })} />
          </Form.Item>
          <Form.Item label="主题材" help="可选，例如：玄幻 / 都市 / 科幻；留空时由 AI 反推">
            <Input value={options.genre} maxLength={100} onChange={(e) => onChange({ ...options, genre: e.target.value })} />
          </Form.Item>
          <Form.Item label="导入范围" help={`导入后章节从 1 开始重新编号；一次最多导入 ${record.max_import_chapters} 章`}>
            <RangeField
              label="导入范围"
              value={options.importRange}
              min={1}
              max={count}
              onChange={(r) => onChange(withImportRange(options, r, deepTouched))}
            />
          </Form.Item>
          <Form.Item label="反推范围" help="AI 只通读这一段来反推设定（须在导入范围内）；导入的正文不受影响">
            <RangeField
              label="反推范围"
              value={options.reverseRange}
              min={options.importRange[0]}
              max={options.importRange[1]}
              onChange={(r) => onChange({ ...options, reverseRange: r })}
            />
          </Form.Item>
          <Form.Item label="可选项">
            <Space direction="vertical">
              <Checkbox checked={options.extractStyle} onChange={(e) => onChange({ ...options, extractStyle: e.target.checked })}>
                提取写作风格（保存为「《书名》原文风格」，项目没有默认风格时设为默认）
              </Checkbox>
              <Checkbox
                checked={options.deepAnalyze}
                onChange={(e) => {
                  onDeepTouched()
                  onChange({ ...options, deepAnalyze: e.target.checked })
                }}
              >
                逐章深度分析（伏笔、角色状态、剧情记忆；导入 ≤ {DEEP_ANALYZE_DEFAULT_MAX} 章时默认勾选，当前 {importCount} 章）
              </Checkbox>
            </Space>
          </Form.Item>
          <Form.Item label="AI 配置" help="留空使用默认文本模型">
            <AIConfigSelect value={options.aiConfigId} onChange={(v) => onChange({ ...options, aiConfigId: v })} />
          </Form.Item>
        </Form>
        {error && <Alert type="error" showIcon message={error} />}
      </Col>
      <Col xs={24} lg={10}>
        <Card size="small" title="预估 AI 输入字数">
          <Spin spinning={estimating}>
            {estimateError && <Alert type="error" showIcon message={estimateError} style={{ marginBottom: 8 }} />}
            {estimate && !error ? (
              <Descriptions size="small" column={1}>
                <Descriptions.Item label="导入">
                  {estimate.import_chapters} 章 · {formatChars(estimate.import_chars)}
                </Descriptions.Item>
                <Descriptions.Item label="反推通读">
                  {formatChars(estimate.reverse_chars)}，{estimate.batches} 批
                </Descriptions.Item>
                <Descriptions.Item label="反推（通读 + 汇总）">
                  {formatChars(estimate.digest_input + estimate.synthesize_input)}
                </Descriptions.Item>
                <Descriptions.Item label="风格提取">{options.extractStyle ? formatChars(estimate.style_input) : '不提取'}</Descriptions.Item>
                <Descriptions.Item label="深度分析">{options.deepAnalyze ? formatChars(estimate.analyze_input) : '不分析'}</Descriptions.Item>
                <Descriptions.Item label="合计">
                  <Typography.Text strong>{formatChars(estimate.total_input)}</Typography.Text>
                </Descriptions.Item>
              </Descriptions>
            ) : (
              !estimateError && <Typography.Text type="secondary">{error ? '选项有误，无法预估' : '正在预估…'}</Typography.Text>
            )}
          </Spin>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
            按字符估算，不含模型输出；实际用量以模型提供方的计费为准。
          </Typography.Paragraph>
        </Card>
      </Col>
    </Row>
  )
}
