import { AutoComplete, Col, Form, Input, Row, Typography } from 'antd'
import { STYLE_LIMITS } from '../../api/styles'
import { sampleLengthHint, STYLE_DIMENSIONS } from '../../utils/styles'

interface Props {
  disabled?: boolean
  /** 名称是否必填（保存时必填；试写的未保存草稿可空） */
  nameRequired?: boolean
}

/** 风格字段表单项（放在 Form 内使用，字段名与 StyleDraft 一致）：维度为「下拉 + 自由填写」，示例段落带字数提示。 */
export default function StyleFormFields({ disabled, nameRequired = true }: Props) {
  return (
    <>
      <Row gutter={16}>
        <Col xs={24} md={10}>
          <Form.Item
            name="name"
            label="名称"
            rules={[
              ...(nameRequired ? [{ required: true, whitespace: true, message: '请输入名称' }] : []),
              { max: STYLE_LIMITS.name, message: `不超过 ${STYLE_LIMITS.name} 字` },
            ]}
          >
            <Input disabled={disabled} />
          </Form.Item>
        </Col>
        <Col xs={24} md={14}>
          <Form.Item
            name="description"
            label="一句话描述"
            tooltip="向导与设定生成只带这一句"
            rules={[{ max: STYLE_LIMITS.description, message: `不超过 ${STYLE_LIMITS.description} 字` }]}
          >
            <Input disabled={disabled} placeholder="例如：克制冷静的白描，信息密度高" />
          </Form.Item>
        </Col>
      </Row>
      <Row gutter={16}>
        {STYLE_DIMENSIONS.map((d) => (
          <Col xs={24} md={12} key={d.key}>
            <Form.Item
              name={d.key}
              label={d.label}
              rules={[{ max: STYLE_LIMITS.dimension, message: `不超过 ${STYLE_LIMITS.dimension} 字` }]}
            >
              <AutoComplete
                disabled={disabled}
                allowClear
                options={d.options.map((v) => ({ value: v }))}
                placeholder="可从下拉选择，也可以自由填写"
                filterOption={(input, option) => String(option?.value ?? '').includes(input)}
              />
            </Form.Item>
          </Col>
        ))}
      </Row>
      <Form.Item
        name="taboos"
        label="禁用表达"
        rules={[{ max: STYLE_LIMITS.taboos, message: `不超过 ${STYLE_LIMITS.taboos} 字` }]}
      >
        <Input.TextArea disabled={disabled} autoSize={{ minRows: 2, maxRows: 6 }} placeholder="不希望出现的套话、句式或词语" />
      </Form.Item>
      <Form.Item noStyle shouldUpdate={(a, b) => a.sample !== b.sample}>
        {({ getFieldValue }) => {
          const hint = sampleLengthHint(String(getFieldValue('sample') ?? ''), STYLE_LIMITS.sample)
          return (
            <Form.Item
              name="sample"
              label="示例段落"
              extra={
                <Typography.Text type={hint.over ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>
                  {hint.text}：给一段体现该风格的文字，模型会模仿它的笔调（不会照抄内容）
                </Typography.Text>
              }
              rules={[{ max: STYLE_LIMITS.sample, message: `不超过 ${STYLE_LIMITS.sample} 字` }]}
            >
              <Input.TextArea disabled={disabled} autoSize={{ minRows: 3, maxRows: 10 }} />
            </Form.Item>
          )
        }}
      </Form.Item>
      <Form.Item
        name="extra"
        label="自由补充"
        rules={[{ max: STYLE_LIMITS.extra, message: `不超过 ${STYLE_LIMITS.extra} 字` }]}
      >
        <Input.TextArea disabled={disabled} autoSize={{ minRows: 2, maxRows: 10 }} placeholder="其他要求，会原样写进风格段" />
      </Form.Item>
    </>
  )
}
