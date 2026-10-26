import { Col, Input, InputNumber, Row, Select, Typography } from 'antd'
import { MAX_EST_WORDS, MIN_EST_WORDS } from '../../api/outlines'

/** 细纲的可编辑字段（展开预览草稿与大纲页行内编辑共用） */
export interface PlanFieldValues {
  title: string
  summary: string
  key_events: string[]
  character_ids: number[]
  emotion: string
  goal: string
  est_words: number
}

interface Props<T extends PlanFieldValues> {
  value: T
  onChange: (patch: Partial<PlanFieldValues>) => void
  /** 角色下拉选项（本项目角色） */
  characterOptions: { value: number; label: string }[]
  disabled?: boolean
}

/** 细纲字段受控编辑：标题、梗概、关键事件（tags）、出场角色、情感基调、叙事目标、预估字数。 */
export default function PlanFieldsEditor<T extends PlanFieldValues>({ value, onChange, characterOptions, disabled }: Props<T>) {
  return (
    <Row gutter={[12, 8]}>
      <Col span={24}>
        <Input
          value={value.title}
          placeholder="细纲标题"
          maxLength={200}
          disabled={disabled}
          status={!value.title.trim() ? 'error' : undefined}
          onChange={(e) => onChange({ title: e.target.value })}
        />
      </Col>
      <Col span={24}>
        <Input.TextArea
          value={value.summary}
          placeholder="本章梗概"
          autoSize={{ minRows: 2, maxRows: 8 }}
          disabled={disabled}
          onChange={(e) => onChange({ summary: e.target.value })}
        />
      </Col>
      <Col xs={24} md={12}>
        <Typography.Text type="secondary">关键事件（回车添加）</Typography.Text>
        <Select
          mode="tags"
          value={value.key_events}
          disabled={disabled}
          style={{ width: '100%' }}
          tokenSeparators={['\n']}
          maxCount={20}
          open={false}
          suffixIcon={null}
          onChange={(v: string[]) => onChange({ key_events: v.map((x) => x.trim().slice(0, 200)).filter(Boolean) })}
        />
      </Col>
      <Col xs={24} md={12}>
        <Typography.Text type="secondary">出场角色</Typography.Text>
        <Select
          mode="multiple"
          allowClear
          value={value.character_ids}
          options={characterOptions}
          optionFilterProp="label"
          disabled={disabled}
          style={{ width: '100%' }}
          onChange={(v: number[]) => onChange({ character_ids: v })}
        />
      </Col>
      <Col xs={24} md={8}>
        <Typography.Text type="secondary">情感基调</Typography.Text>
        <Input
          value={value.emotion}
          maxLength={100}
          disabled={disabled}
          onChange={(e) => onChange({ emotion: e.target.value })}
        />
      </Col>
      <Col xs={24} md={10}>
        <Typography.Text type="secondary">叙事目标</Typography.Text>
        <Input.TextArea
          value={value.goal}
          autoSize={{ minRows: 1, maxRows: 4 }}
          disabled={disabled}
          onChange={(e) => onChange({ goal: e.target.value })}
        />
      </Col>
      <Col xs={24} md={6}>
        <Typography.Text type="secondary">预估字数</Typography.Text>
        <InputNumber
          min={MIN_EST_WORDS}
          max={MAX_EST_WORDS}
          step={500}
          value={value.est_words}
          disabled={disabled}
          style={{ width: '100%' }}
          onChange={(v) => onChange({ est_words: v ?? 3000 })}
        />
      </Col>
    </Row>
  )
}
