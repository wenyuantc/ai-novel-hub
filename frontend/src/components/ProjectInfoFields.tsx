import { Form, Input, InputNumber, Select } from 'antd'
import { normalizeTags, PROJECT_FIELD_LIMITS as L, tagsError } from '../utils/projectFields'

/** 立项结构化字段的表单项（项目概览、新建项目弹窗共用）：需放在 Form 内，字段名与 ProjectCreate 一致。 */
export default function ProjectInfoFields() {
  return (
    <>
      <Form.Item
        name="tags"
        label="类型标签"
        extra={`最多 ${L.maxTags} 个，每个不超过 ${L.tagMaxLen} 字；回车添加，首尾空白与重复项会自动去掉`}
        normalize={(v: unknown) => (Array.isArray(v) ? normalizeTags(v) : [])}
        rules={[
          {
            validator: async (_, value: unknown) => {
              const err = tagsError(Array.isArray(value) ? value : [])
              if (err) throw new Error(err)
            },
          },
        ]}
      >
        <Select mode="tags" open={false} suffixIcon={null} tokenSeparators={[',', '，', '、']} placeholder="例如：修仙、穿越、群像" />
      </Form.Item>
      <Form.Item name="theme" label="主题" rules={[{ max: L.theme, message: `不超过 ${L.theme} 字` }]}>
        <Input placeholder="例如：成长与代价、个人与体制" />
      </Form.Item>
      <Form.Item name="synopsis" label="简介" rules={[{ max: L.synopsis, message: `不超过 ${L.synopsis} 字` }]}>
        <Input.TextArea autoSize={{ minRows: 3, maxRows: 10 }} placeholder="作品简介，类似书封文案" />
      </Form.Item>
      <Form.Item
        name="selling_points"
        label="核心卖点"
        rules={[{ max: L.sellingPoints, message: `不超过 ${L.sellingPoints} 字` }]}
      >
        <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} placeholder="读者为什么会追这本书" />
      </Form.Item>
      <Form.Item name="audience" label="目标读者" rules={[{ max: L.audience, message: `不超过 ${L.audience} 字` }]}>
        <Input placeholder="例如：喜欢慢热成长线的男频读者" />
      </Form.Item>
      <Form.Item
        name="target_words"
        label="目标总字数"
        extra="只用于进度显示与向导参数建议，不进提示词；清空 = 不设目标"
      >
        <InputNumber
          min={L.targetWordsMin}
          max={L.targetWordsMax}
          step={10000}
          precision={0}
          style={{ width: 240 }}
          placeholder="例如 300000"
          addonAfter="字"
        />
      </Form.Item>
    </>
  )
}
