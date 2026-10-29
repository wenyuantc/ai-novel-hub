import { Form, Select } from 'antd'
import { normalizeWorkshopTags, workshopCategoryOptions, workshopTagsError } from '../../utils/workshop'

/** 提交、审核及管理员编辑共用分类与标签约束。 */
export default function WorkshopMetadataFields({ disabled = false }: { disabled?: boolean }) {
  return <>
    <Form.Item name="category" label="分类" rules={[{ required: true, message: '请选择分类' }]}>
      <Select options={workshopCategoryOptions} disabled={disabled} />
    </Form.Item>
    <Form.Item name="tags" label="标签" normalize={normalizeWorkshopTags} extra="最多 5 个，每个 1–12 字"
      rules={[{ validator: (_, value: string[] = []) => {
        const error = workshopTagsError(value)
        return error ? Promise.reject(new Error(error)) : Promise.resolve()
      } }]}>
      <Select mode="tags" open={false} tokenSeparators={[',', '，', '、']} disabled={disabled} placeholder="输入后按回车添加" />
    </Form.Item>
  </>
}
