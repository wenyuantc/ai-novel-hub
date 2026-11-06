import { Collapse } from 'antd'
import type { SettingEntryRef } from '../../api/types'
import EntityMentions from '../mentions/EntityMentions'
import SettingAiPanel from './SettingAiPanel'
import type { SettingAiSession } from './settingAiController'

/** 非标签页详情共用的折叠入口；收起时停止读取，未保存设置仍留在会话。 */
export default function SettingAiDetailSections({ session, entry }: { session: SettingAiSession; entry: SettingEntryRef }) {
  return <Collapse size="small" destroyOnHidden items={[
    { key: 'visibility', label: 'AI 与提及', children: <SettingAiPanel session={session} entry={entry} /> },
    { key: 'mentions', label: '提及记录', children: <EntityMentions projectId={session.projectId} entity={entry} /> },
  ]} />
}
