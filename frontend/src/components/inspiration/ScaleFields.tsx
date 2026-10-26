import { Col, Form, InputNumber, Row, Typography } from 'antd'
import type { FormInstance } from 'antd'
import { useEffect, useRef } from 'react'
import { inspirationApi } from '../../api/inspiration'
import { scaleText, suggestScale, type ScaleFormValues } from '../../utils/inspiration'
import { PROJECT_FIELD_LIMITS as L } from '../../utils/projectFields'

/**
 * 规模字段（规模一步与确认一步共用）：修改目标总字数后（400ms 防抖）调用 /scale-suggest 重新给出卷数、每卷章数与角色数，
 * 数字都可以再手动修改；即时提示用本地 suggestScale（与后端规则一致）。
 */
export default function ScaleFields({ form, disabled }: { form: FormInstance<ScaleFormValues>; disabled?: boolean }) {
  const target = Form.useWatch('target_words', form)
  // 记录上次据以建议的字数：挂载时的初始值不触发建议，避免覆盖已保存的规模
  const lastRef = useRef<number | null | undefined>(form.getFieldValue('target_words') as number | null | undefined)

  useEffect(() => {
    if (target === undefined || target === lastRef.current) return
    lastRef.current = target
    if (!target || target < L.targetWordsMin || target > L.targetWordsMax) return
    let alive = true
    const timer = setTimeout(() => {
      inspirationApi
        .scaleSuggest(target)
        .then((s) => {
          if (alive) form.setFieldsValue({ volume_count: s.volume_count, chapters_per_volume: s.chapters_per_volume, character_count: s.character_count })
        })
        .catch(() => {
          // 建议失败时退回本地规则
          if (alive) {
            const s = suggestScale(target)
            form.setFieldsValue({ volume_count: s.volume_count, chapters_per_volume: s.chapters_per_volume, character_count: s.character_count })
          }
        })
    }, 400)
    return () => {
      alive = false
      clearTimeout(timer)
    }
  }, [target, form])

  const hint = target && target >= L.targetWordsMin && target <= L.targetWordsMax ? suggestScale(target) : null

  return (
    <>
      <Form.Item
        name="target_words"
        label="目标总字数"
        extra={
          hint ? (
            <Typography.Text type="secondary">
              建议：{scaleText(hint)}（每章约 3000 字、每卷约 30 章）。修改字数会重新给出建议，下面的数字都可以再改。
            </Typography.Text>
          ) : (
            '填写后自动建议卷数、每卷章数与角色数；不填也可以，手动设置下面的数字'
          )
        }
      >
        <InputNumber
          min={L.targetWordsMin}
          max={L.targetWordsMax}
          step={10000}
          precision={0}
          style={{ width: 240 }}
          addonAfter="字"
          disabled={disabled}
        />
      </Form.Item>
      <Row gutter={16}>
        <Col span={8}>
          <Form.Item name="volume_count" label="卷数" rules={[{ required: true, message: '请输入卷数' }]}>
            <InputNumber min={1} max={20} precision={0} style={{ width: '100%' }} disabled={disabled} />
          </Form.Item>
        </Col>
        <Col span={8}>
          <Form.Item name="chapters_per_volume" label="每卷章数" rules={[{ required: true, message: '请输入每卷章数' }]}>
            <InputNumber min={1} max={100} precision={0} style={{ width: '100%' }} disabled={disabled} />
          </Form.Item>
        </Col>
        <Col span={8}>
          <Form.Item name="character_count" label="角色数" rules={[{ required: true, message: '请输入角色数' }]}>
            <InputNumber min={3} max={30} precision={0} style={{ width: '100%' }} disabled={disabled} />
          </Form.Item>
        </Col>
      </Row>
    </>
  )
}
