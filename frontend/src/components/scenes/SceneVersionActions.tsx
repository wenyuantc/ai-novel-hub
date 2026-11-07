import { Alert, Button, Modal, Select, Space, Typography } from 'antd'
import { useState } from 'react'
import { versionApi } from '../../api/chapterVersions'
import { chapterApi } from '../../api/chapters'
import { errorMessage } from '../../api/client'
import type { ChapterVersion, ChapterVersionBrief, SceneBrief, SceneExtractPreview, SceneRestorePreview } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import TextDiff from '../TextDiff'
import { sceneLabel } from './scenePresentation'

export interface VersionSceneContext {
  scenes: SceneBrief[]
  currentSceneId: number | null
  prepare: () => Promise<number | null>
  onExtract: (version: ChapterVersionBrief, preview: SceneExtractPreview) => Promise<boolean>
}
interface Props { projectId: number; chapterId: number; version: ChapterVersionBrief; context?: VersionSceneContext; disabled: boolean; onRestore: (version: ChapterVersionBrief, revision?: number) => Promise<boolean>; onRestored: () => void }
/** 结构恢复先预检引用；多场景旧纯文本只能明确提取整份旧稿到一个目标。 */
export default function SceneVersionActions({ projectId, chapterId, version, context, disabled, onRestore, onRestored }: Props) {
  const access = useProjectAccess(projectId)
  const [open, setOpen] = useState(false)
  const [target, setTarget] = useState<number | undefined>(context?.currentSceneId ?? undefined)
  const [restore, setRestore] = useState<SceneRestorePreview>()
  const [versionDetails, setVersionDetails] = useState<ChapterVersion>()
  const [currentBody, setCurrentBody] = useState('')
  const [extract, setExtract] = useState<SceneExtractPreview>()
  const [working, setWorking] = useState(false)
  const [error, setError] = useState('')
  const legacyExtraction = !version.has_scene_snapshot && (context?.scenes.length ?? 1) > 1
  const begin = async () => {
    if (!access.canEditNow() || disabled) return
    setOpen(true); setError(''); setRestore(undefined); setExtract(undefined); setVersionDetails(undefined)
    if (legacyExtraction) return
    setWorking(true)
    try {
      const revision = context ? await context.prepare() : undefined
      if (revision === null || !access.canEditNow()) { setError('请先保存当前草稿并完成版本冲突核对'); return }
      if (revision !== undefined) {
        const preview = await versionApi.restorePreview(projectId, chapterId, version.id, revision)
        const [full, current] = await Promise.all([versionApi.get(projectId, chapterId, version.id), chapterApi.get(projectId, chapterId)])
        if (current.content_revision !== preview.content_revision || preview.content_revision !== revision) throw new Error('读取恢复预览期间正文又有变化，请重新核对')
        if (access.canReadNow()) { setRestore(preview); setVersionDetails(full); setCurrentBody(current.content) }
      }
    } catch (err) { if (access.canReadNow()) setError(errorMessage(err)) }
    finally { if (access.canReadNow()) setWorking(false) }
  }
  const previewExtraction = async () => {
    if (!context || !target || !access.canEditNow()) return
    setWorking(true); setError(''); setExtract(undefined)
    try {
      const revision = await context.prepare()
      if (revision === null || !access.canEditNow()) { setError('请先保存当前草稿并完成版本冲突核对'); return }
      const preview = await versionApi.extractPreview(projectId, chapterId, version.id, target, revision)
      if (access.canReadNow() && preview.scene_id === target) setExtract(preview)
    } catch (err) { if (access.canReadNow()) setError(errorMessage(err)) }
    finally { if (access.canReadNow()) setWorking(false) }
  }
  const apply = async () => {
    if (!access.canEditNow() || disabled || working) return
    setWorking(true); setError('')
    try {
      const success = legacyExtraction ? !!context && !!extract && await context.onExtract(version, extract) : await onRestore(version, restore?.content_revision)
      if (success && access.canEditNow()) { setOpen(false); onRestored() }
      else if (access.canReadNow()) setError('本次操作未完成，原预览与选择已保留。请先核对错误，再重新打开预检；不会自动更新版本重试。')
    } catch (err) { if (access.canReadNow()) setError(errorMessage(err)) }
    finally { if (access.canReadNow()) setWorking(false) }
  }
  return <>
    <Button type="link" size="small" disabled={disabled || !access.canEdit} onClick={() => void begin()}>{legacyExtraction ? '提取到场景' : '恢复'}</Button>
    <Modal open={open} title={legacyExtraction ? `提取 v${version.seq} 旧纯文本到一个场景` : `恢复 v${version.seq}${version.has_scene_snapshot ? ' 场景结构' : ' 正文'}`} onCancel={() => { if (!working) setOpen(false) }} maskClosable={false} width={860}
      footer={<Space><Button disabled={working} onClick={() => setOpen(false)}>取消</Button><Button type="primary" loading={working} disabled={disabled || !access.canEdit || !!error || (legacyExtraction ? !extract : !!context && (!restore || restore.blockers.length > 0))} onClick={() => void apply()}>{legacyExtraction ? '确认替换目标场景正文' : '确认恢复'}</Button></Space>}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {legacyExtraction ? <><Alert type="warning" showIcon message="这不是整章恢复" description="将整份旧稿提取并替换所选场景正文；其他场景、摘要与规划不变。请先查看差异，再明确确认。" /><Select aria-label="旧稿提取目标场景" style={{ width: '100%' }} value={target} options={context?.scenes.map((scene) => ({ value: scene.id, label: sceneLabel(scene) }))} disabled={working} onChange={(sid) => { setTarget(sid); setExtract(undefined) }} /><Button loading={working} disabled={!target} onClick={() => void previewExtraction()}>预览目标场景与完整旧稿差异</Button>{extract && <TextDiff a={extract.original} b={extract.replacement} maxHeight={400} />}</> : <><Typography.Paragraph>当前内容先保留历史，恢复自身也生成新版本。{version.has_scene_snapshot ? '恢复场景正文、摘要、顺序与本地覆盖，不回滚章默认、独立支线节点、交汇或时间线事实。' : '当前单场景正常恢复旧正文，不虚构旧结构。'}</Typography.Paragraph>{restore && <><Typography.Text>目标场景：{restore.scene_ids.join('、') || '无'}；将退场：{restore.retiring_scene_ids.join('、') || '无'}</Typography.Text>{restore.blockers.map((item) => <Alert type="error" key={`${item.kind}:${item.id}`} message={item.label} />)}</>}</>}
        {!legacyExtraction && versionDetails && <><Typography.Text strong>当前整章 → 目标版本正文差异</Typography.Text><TextDiff a={currentBody} b={versionDetails.content} maxHeight={300} />{versionDetails.scene_snapshot && <Space direction="vertical" style={{ width: '100%' }}><Typography.Text strong>结构与本地元数据：{context?.scenes.length ?? 0} → {versionDetails.scene_snapshot.scenes.length} 个场景</Typography.Text>{versionDetails.scene_snapshot.scenes.map((target, index) => {
          const current = context?.scenes.find((scene) => scene.id === target.id)
          return <div key={target.id}><Typography.Text>场景 {index + 1} · #{target.id}：{current?.title || '未命名／当前未活动'} → {target.title || '未命名'}</Typography.Text><Typography.Paragraph style={{ whiteSpace: 'pre-wrap', margin: 0 }}>摘要：{current?.summary || '无'} → {target.summary || '无'}</Typography.Paragraph><Typography.Text type="secondary">目标本地模式：{target.planning ? Object.entries(target.planning.raw.modes).map(([field, mode]) => `${field}=${mode === 'inherit' ? '继承' : '覆盖'}`).join('；') : '全部继承本章'}</Typography.Text></div>
        })}</Space>}</>}
        {error && <Alert type="error" showIcon message={error} description="输入与预览已保留；请先核对最新版本，再重新打开预检，不会自动重试覆盖。" />}
      </Space>
    </Modal>
  </>
}
