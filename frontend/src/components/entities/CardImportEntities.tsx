import { List, Radio, Space, Tag, Typography } from 'antd'
import { importedName, summarizeImport } from '../../api/cards'
import type { CardPreview, CardPreviewItem, CardResolution, CardResolutions } from '../../api/types'
import type { CardEntryGroup } from './cardImportMapping'

interface Props { preview: CardPreview; choices: CardResolutions; v4: boolean; disabled: boolean; onChange: (group: CardEntryGroup, name: string, value: CardResolution) => void }

function ResolutionChoice({ value, renameTo, disabled, v4, onChange }: { value: CardResolution; renameTo: string; disabled: boolean; v4: boolean; onChange: (value: CardResolution) => void }) {
  return <Radio.Group value={value} disabled={disabled} style={{ width: '100%' }} onChange={(event) => { const next: unknown = event.target.value; if (next === 'skip' || next === 'rename') onChange(next) }}>
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Radio value="skip">{v4 ? '跳过内容，保留既有条目' : '跳过（关联已有）'}</Radio>
      <Radio value="rename" style={{ maxWidth: '100%', whiteSpace: 'normal', overflowWrap: 'anywhere' }}>重命名新建：{renameTo}</Radio>
    </Space>
  </Radio.Group>
}

/** 旧版名称关联继续保留；v4跳过只跳过内容，typed引用另行显式映射。 */
export default function CardImportEntities({ preview, choices, v4, disabled, onChange }: Props) {
  const section = (group: 'characters' | 'organizations', title: string, items: CardPreviewItem[]) => {
    const summary = summarizeImport(items, choices[group])
    return <section aria-label={`卡片${title}处理`}>
      <Typography.Title level={5}>{title}（新建 {summary.created}，重命名 {summary.renamed}，跳过 {summary.skipped}）</Typography.Title>
      <List size="small" dataSource={items} pagination={items.length > 20 ? { pageSize: 20, size: 'small' } : false}
        locale={{ emptyText: `无${title}` }} renderItem={(item) => <List.Item key={item.name}>
          <Space direction="vertical" size={8} style={{ width: '100%', minWidth: 0 }}>
            <Space wrap><Typography.Text strong style={{ overflowWrap: 'anywhere' }}>{item.name}</Typography.Text><Tag>{item.status === 'new' ? '新增' : '同名冲突'}</Tag></Space>
            {item.status === 'new' ? <Typography.Text type="secondary">直接导入</Typography.Text>
              : <ResolutionChoice value={choices[group][item.name] ?? 'skip'} renameTo={item.rename_to ?? item.name} disabled={disabled} v4={v4} onChange={(value) => onChange(group, item.name, value)} />}
            <Typography.Text type="secondary">导入后名称：{importedName(item, choices[group][item.name])}</Typography.Text>
          </Space>
        </List.Item>} />
    </section>
  }
  return <Space direction="vertical" size={16} style={{ width: '100%' }}>
    {section('characters', '角色', preview.characters)}
    {section('organizations', '组织', preview.organizations)}
    {!!preview.careers.length && <section aria-label="卡片职业处理">
      <Typography.Title level={5}>职业（{preview.careers.length}）</Typography.Title>
      <List size="small" dataSource={preview.careers} renderItem={(item) => <List.Item key={item.name}>
        <Space direction="vertical" size={8} style={{ width: '100%', minWidth: 0 }}>
          <Typography.Text strong style={{ overflowWrap: 'anywhere' }}>{item.name}</Typography.Text>
          {item.status === 'new' ? <Typography.Text type="secondary">按卡片阶梯新建</Typography.Text>
            : item.status === 'reuse' ? <Typography.Text type="secondary">复用同名职业；{item.stages_added.length ? `追加到阶梯末尾：${item.stages_added.join('、')}` : '阶段均已存在'}</Typography.Text>
              : <ResolutionChoice value={choices.careers?.[item.name] ?? 'skip'} renameTo={item.rename_to ?? `${item.name}（导入）`} disabled={disabled} v4 onChange={(value) => onChange('careers', item.name, value)} />}
        </Space>
      </List.Item>} />
    </section>}
    {!!preview.relationships.length && <section aria-label="卡片人物关系">
      <Typography.Title level={5}>人物关系（{preview.relationships.length}）</Typography.Title>
      <List size="small" dataSource={preview.relationships} pagination={{ pageSize: 8, hideOnSinglePage: true }} renderItem={(item) => <List.Item>
        <Space wrap><span>{item.a_name} 是 {item.b_name} 的{item.role_a}</span><Tag>{item.status === 'ok' ? '可关联' : '无法关联'}</Tag></Space>
      </List.Item>} />
    </section>}
  </Space>
}
