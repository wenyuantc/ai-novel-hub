import { Alert, Button, Card, Checkbox, Drawer, Popconfirm, Radio, Skeleton, Space, Tag, theme, Tooltip, Typography } from 'antd'
import { useEffect, useRef, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { ImpactAssessment, ImpactDetail, ImpactItem, ImpactItemStatus } from '../../api/consistency'
import { useModalWidth } from '../../hooks/useIsMobile'
import { evidenceLink, ISSUE_CATEGORY_LABELS } from '../../utils/consistency'
import {
  changeSides, chapterLabel, evidenceOverflowNote, IMPACT_CHANGE_KIND, IMPACT_ITEM_STATUS, isAcknowledged, itemStatus,
  LEDGER_STATE, memoriesHref, pendingCount, PROGRESSION_KIND_LABELS, progressionHref, setAcknowledged, setItemStatus,
  summaryHref, uncoveredCheckRange, type AssessGate, type CheckRange, type ImpactEdits,
} from '../../utils/consistencyImpact'
import { MEMORY_KIND_LABELS } from '../../utils/kb'
import { formatTime } from '../../utils/time'

const wrap = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } as const

function ItemCard({ projectId, item, status, canConfirm, canEdit, disabled, onStatus }: {
  projectId: number; item: ImpactItem; status: ImpactItemStatus; canConfirm: boolean; canEdit: boolean; disabled: boolean
  onStatus: (status: ImpactItemStatus) => void
}) {
  const { token } = theme.useToken()
  const overflow = evidenceOverflowNote(item)
  return <Card size="small" title={chapterLabel(item)} styles={{ body: { minWidth: 0 } }}
    extra={<Tag color={IMPACT_ITEM_STATUS[status].color} style={{ marginInlineEnd: 0 }}>{IMPACT_ITEM_STATUS[status].label}</Tag>}>
    <Space direction="vertical" size="small" style={{ width: '100%', minWidth: 0 }}>
      {item.changed && <Tag color="warning" style={{ whiteSpace: 'normal' }}>
        {item.chapter_no === null ? '该章已被删除' : '该章在评估后已修改，下面的原文与位置可能已失效'}
      </Tag>}
      {item.subjects.length > 0 && <Typography.Text style={{ overflowWrap: 'anywhere' }}>涉及：{item.subjects.join('、')}</Typography.Text>}
      {item.evidence.length === 0 && <Typography.Text type="secondary">没有记录相关原文</Typography.Text>}
      {item.evidence.map((evidence, index) => {
        const link = evidenceLink(projectId, {
          chapter_id: item.chapter_id, chapter_no: item.chapter_no, chapter_title: item.chapter_title,
          start: evidence.start, end: evidence.end, text_hash: evidence.text_hash, stale: item.changed,
        })
        return <div key={`${evidence.fact_id}:${index}`} style={{ minWidth: 0 }}>
          <blockquote style={{ margin: 0, paddingInlineStart: 12, borderInlineStart: `3px solid ${token.colorBorder}`, ...wrap }}>
            {evidence.quote || '（无引句）'}
          </blockquote>
          <Space size={[8, 4]} wrap style={{ marginTop: 4 }}>
            {link.notes.map((note) => <Tag key={note} color={link.kind === 'deleted' ? 'default' : 'warning'} style={{ whiteSpace: 'normal' }}>{note}</Tag>)}
            {link.kind !== 'deleted' && <>
              <Link to={link.readerHref}>{link.kind === 'anchored' ? '在阅读器中定位' : '在阅读器中打开该章'}</Link>
              {canEdit && <Link to={link.editorHref}>{link.kind === 'anchored' ? '在编辑器中定位' : '在编辑器中打开该章'}</Link>}
            </>}
          </Space>
        </div>
      })}
      {overflow && <Typography.Text type="secondary">{overflow}</Typography.Text>}
      {canConfirm && <Radio.Group aria-label={`${chapterLabel(item)}的复核状态`} value={status} disabled={disabled}
        onChange={(event) => onStatus(event.target.value as ImpactItemStatus)}
        options={(Object.keys(IMPACT_ITEM_STATUS) as ImpactItemStatus[]).map((value) => ({ value, label: IMPACT_ITEM_STATUS[value].label }))} />}
    </Space>
  </Card>
}

function AssessmentView({ projectId, detail, assessment, edits, canConfirm, canEdit, canStartCheck, checkBlocked, saving, onEdits, onRequestCheck }: {
  projectId: number; detail: ImpactDetail; assessment: ImpactAssessment; edits: ImpactEdits; canConfirm: boolean; canEdit: boolean
  canStartCheck: boolean; checkBlocked: string; saving: boolean
  onEdits: (edits: ImpactEdits) => void; onRequestCheck: (range: CheckRange | null) => void
}) {
  const range = uncoveredCheckRange(assessment.uncovered)
  const pending = pendingCount(detail, edits)
  return <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
    <Typography.Text type="secondary">
      评估于 {assessment.assessed_at ? formatTime(assessment.assessed_at) : '—'}{assessment.model ? ` · 模型 ${assessment.model}` : ''}
    </Typography.Text>
    {assessment.outdated && <Alert type="warning" showIcon message="评估后正文又有修改"
      description="下面是针对上一次改动的评估结果，可能已不反映当前正文。需要时请重新评估。" />}
    {assessment.no_change
      ? <Alert type="success" showIcon message="未发现剧情事实变化" description="本章改动前后的事实没有实质差异（措辞调整不算），无需复核后续章节。" />
      : <section aria-label="事实变化">
        <Typography.Title level={5}>事实变化（{assessment.changes.length} 处）</Typography.Title>
        <Space direction="vertical" size="small" style={{ width: '100%', minWidth: 0 }}>
          {assessment.changes.map((change, index) => {
            const sides = changeSides(change)
            return <Card key={`${change.kind}:${change.subject_key}:${index}`} size="small" styles={{ body: { minWidth: 0 } }}>
              <Space size={[4, 4]} wrap style={{ marginBottom: 4 }}>
                <Tag color={IMPACT_CHANGE_KIND[change.kind].color}>{IMPACT_CHANGE_KIND[change.kind].label}</Tag>
                <Tag>{ISSUE_CATEGORY_LABELS[change.category] ?? change.category}</Tag>
                <Typography.Text strong style={{ overflowWrap: 'anywhere' }}>{change.subject || '（未指明主体）'}</Typography.Text>
              </Space>
              {sides.before !== null && <Typography.Paragraph style={{ margin: 0, ...wrap }}>
                <Typography.Text type="secondary">改动前：</Typography.Text>{sides.before || '（无）'}
              </Typography.Paragraph>}
              {sides.after !== null && <Typography.Paragraph style={{ margin: 0, ...wrap }}>
                <Typography.Text type="secondary">改动后：</Typography.Text>{sides.after || '（无）'}
              </Typography.Paragraph>}
            </Card>
          })}
        </Space>
      </section>}
    {!assessment.no_change && <section aria-label="可能受影响的后续章节">
      <Typography.Title level={5}>可能受影响的后续章节（{assessment.items.length} 章{assessment.items.length > 0 ? `，待复核 ${pending} 项` : ''}）</Typography.Title>
      <Typography.Paragraph type="secondary">
        这些章节的事实台账提到了发生变化的主体，属于「可能受影响」，供复核参考；是否需要修改由你判断，系统不会自动修改任何内容。
      </Typography.Paragraph>
      <Space direction="vertical" size="small" style={{ width: '100%', minWidth: 0 }}>
        {assessment.items.length === 0 && <Typography.Text type="secondary">在已抽取事实的后续章节中没有找到提到这些主体的内容。</Typography.Text>}
        {assessment.items.map((item) => <ItemCard key={item.chapter_id} projectId={projectId} item={item} status={itemStatus(item, edits)}
          canConfirm={canConfirm} canEdit={canEdit} disabled={saving} onStatus={(status) => onEdits(setItemStatus(edits, item.chapter_id, status))} />)}
      </Space>
    </section>}
    {assessment.uncovered.length > 0 && <Alert type="warning" showIcon message={`有 ${assessment.uncovered.length} 章尚未抽取或已过期，无法判断`}
      description={<Space direction="vertical" size={4} style={{ width: '100%', minWidth: 0 }}>
        <span style={{ overflowWrap: 'anywhere' }}>
          {assessment.uncovered.slice(0, 20).map((chapter) => chapterLabel(chapter)).join('、')}
          {assessment.uncovered.length > 20 ? ` 等 ${assessment.uncovered.length} 章` : ''}
        </span>
        <span>这些章节在评估时没有可用的事实台账，是否受影响未知。运行一致性检查抽取事实后，重新评估即可覆盖。</span>
      </Space>}
      action={canStartCheck && range && (checkBlocked
        ? <Tooltip title={checkBlocked}><Button disabled>对这些章节运行一致性检查</Button></Tooltip>
        : <Button onClick={() => onRequestCheck(range)}>对这些章节运行一致性检查</Button>)} />}
  </Space>
}

interface Props {
  projectId: number
  /** 所选章节；null = 关闭 */
  chapterId: number | null
  detail: ImpactDetail | null
  error: string
  edits: ImpactEdits
  dirty: boolean
  conflict: boolean
  saving: boolean
  refreshing: boolean
  assessing: boolean
  canConfirm: boolean
  canAssess: boolean
  canStartCheck: boolean
  /** 可在编辑器中打开章节 */
  canEdit: boolean
  gate: AssessGate
  /** 不能发起检查的原因（已有检查或评估在进行、未配置模型等）；空串 = 可以 */
  checkBlocked: string
  /** 本章评估的实时进度；没有在评估本章时为 null */
  progress: ReactNode
  onEdits: (edits: ImpactEdits) => void
  onSave: () => void
  onRefresh: () => void
  onAssess: () => void
  /** 打开发起检查弹窗并预填范围 */
  onRequestCheck: (range: CheckRange | null) => void
  onClose: () => void
  /** 抽屉关闭动画结束后调用（页面把焦点还给打开它的列表条目） */
  onClosed?: () => void
}

/** 单章的修改影响：第一级派生内容过期清单与跳转、评估影响、事实变化、受影响章节与逐项确认（CAS，冲突保留本地勾选）。 */
export default function ImpactDrawer({
  projectId, chapterId, detail, error, edits, dirty, conflict, saving, refreshing, assessing, canConfirm, canAssess, canStartCheck,
  canEdit, gate, checkBlocked, progress, onEdits, onSave, onRefresh, onAssess, onRequestCheck, onClose, onClosed,
}: Props) {
  const width = useModalWidth(760)
  // 保存完成后「保存确认」因没有未保存修改而禁用，浏览器会把焦点丢到页面主体（Esc 关不掉抽屉）：
  // 此时把焦点放回抽屉内第一个可操作的控件；用户已把焦点移到别处时不打扰。
  const bodyRef = useRef<HTMLDivElement>(null)
  const wasSaving = useRef(false)
  useEffect(() => {
    const finished = wasSaving.current && !saving
    wasSaving.current = saving
    if (!finished) return
    const active = document.activeElement
    const lost = !active || active === document.body || (active instanceof HTMLButtonElement && active.disabled)
    if (lost) bodyRef.current?.querySelector<HTMLElement>('a[href], input:not([disabled]), button:not([disabled])')?.focus()
  }, [saving])
  const assessment = detail?.assessment?.assessed ? detail.assessment : null
  const nothingStale = !!detail && !detail.stale_summary && detail.stale_memories.length === 0 && detail.stale_progressions.length === 0
  const checkButton = (label: string, range: CheckRange | null) => canStartCheck && (checkBlocked
    ? <Tooltip title={checkBlocked}><Button disabled>{label}</Button></Tooltip>
    : <Button onClick={() => onRequestCheck(range)}>{label}</Button>)
  return <Drawer open={chapterId !== null} width={width} title={detail ? `修改影响 · ${chapterLabel(detail)}` : '修改影响'} onClose={onClose} destroyOnHidden
    afterOpenChange={(open) => { if (!open) onClosed?.() }}
    extra={<Button onClick={onRefresh} loading={refreshing}>刷新</Button>}
    footer={canConfirm && detail ? <Space wrap>
      <Button type="primary" onClick={onSave} loading={saving} disabled={!dirty}>保存确认</Button>
      <Typography.Text type="secondary">{dirty ? '有尚未保存的确认' : '确认状态已保存'}</Typography.Text>
    </Space> : undefined}>
    {error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message="修改影响加载失败" description={error} action={<Button onClick={onRefresh}>重试</Button>} />}
    {!detail && !error && <Skeleton active paragraph={{ rows: 8 }} />}
    {detail && <div ref={bodyRef}><Space direction="vertical" size="large" style={{ width: '100%', minWidth: 0 }}>
      <Space size={[4, 4]} wrap>
        <Tag color={LEDGER_STATE[detail.ledger_state].color}>{LEDGER_STATE[detail.ledger_state].label}</Tag>
        {!detail.analyzed && <Tag>尚未分析</Tag>}
        <Link to={`/projects/${projectId}/${canEdit ? 'chapters' : 'read'}/${detail.chapter_id}`}>打开本章</Link>
      </Space>
      {conflict && <Alert type="error" showIcon message="确认状态已被其他人或一次新的评估更新"
        description="你的勾选仍保留在页面上。请先刷新读取最新内容，核对后再保存。"
        action={<Button onClick={onRefresh} loading={refreshing}>刷新</Button>} />}

      <section aria-label="可能过期的派生内容">
        <Typography.Title level={5}>可能过期的派生内容（{detail.stale_count} 项）</Typography.Title>
        <Typography.Paragraph type="secondary">
          本章正文修改后，由旧正文得到的摘要、剧情记忆与设定变化可能不再准确。这里只按正文指纹判断，不调用模型。
          重新分析本章可更新章摘要与剧情记忆；设定变化请到对应设定中核对，确认仍然成立的可勾选「已确认无需更新」。
        </Typography.Paragraph>
        {nothingStale
          ? <Typography.Text type="secondary">{detail.analyzed ? '没有过期的派生内容。' : '本章尚未分析，没有由分析得到的摘要、记忆或设定变化。'}</Typography.Text>
          : <Space direction="vertical" size="small" style={{ width: '100%', minWidth: 0 }}>
            {detail.stale_summary && <Card size="small" title="章摘要" styles={{ body: { minWidth: 0 } }}>
              <Space size={[8, 4]} wrap>
                <span>摘要来自 {formatTime(detail.stale_summary.analyzed_at)} 的分析，此后正文已修改。</span>
                {canEdit && <Link to={summaryHref(projectId, detail.chapter_id)}>打开本章分析</Link>}
              </Space>
            </Card>}
            {detail.stale_memories.length > 0 && <Card size="small" title={`剧情记忆（${detail.stale_memories.length} 条）`} styles={{ body: { minWidth: 0 } }}
              extra={<Link to={memoriesHref(projectId)}>到剧情记忆页查看</Link>}>
              <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                {detail.stale_memories.map((memory) => <li key={memory.id} style={wrap}>
                  <Tag>{(MEMORY_KIND_LABELS as Record<string, string>)[memory.kind] ?? memory.kind}</Tag>{memory.content}
                </li>)}
              </ul>
            </Card>}
            {detail.stale_progressions.length > 0 && <Card size="small" title={`设定变化（${detail.stale_progressions.length} 条）`} styles={{ body: { minWidth: 0 } }}>
              <Space direction="vertical" size="small" style={{ width: '100%', minWidth: 0 }}>
                {detail.stale_progressions.map((item) => {
                  const acknowledged = isAcknowledged(item, edits)
                  return <div key={item.id} style={{ minWidth: 0 }}>
                    <Space size={[8, 4]} wrap>
                      <Tag>{PROGRESSION_KIND_LABELS[item.subject_kind] ?? item.subject_kind}</Tag>
                      <Typography.Text strong style={{ overflowWrap: 'anywhere' }}>{item.subject_name || '（主体已删除）'}</Typography.Text>
                      {item.anchor_label && <Typography.Text type="secondary">{item.anchor_label}</Typography.Text>}
                      <Link to={progressionHref(projectId, detail.chapter_id, item)}>查看设定变化</Link>
                    </Space>
                    <div>
                      {canConfirm
                        ? <Checkbox checked={acknowledged} disabled={saving} onChange={(event) => onEdits(setAcknowledged(edits, item.id, event.target.checked))}>已确认无需更新</Checkbox>
                        : acknowledged && <Tag color="success">已确认无需更新</Tag>}
                    </div>
                  </div>
                })}
              </Space>
            </Card>}
          </Space>}
      </section>

      <section aria-label="评估影响">
        <Typography.Title level={5}>评估对后续章节的影响</Typography.Title>
        <Typography.Paragraph type="secondary">
          重新抽取本章事实并与改动前比较，找出新增、消失、改变的剧情事实，再列出提到相关主体的后续章节。最多调用两次模型；
          评估不会改动正文、摘要、剧情记忆、设定，也不会改变潜在冲突清单。
        </Typography.Paragraph>
        {progress}
        {gate.reason && <Alert type="info" showIcon style={{ marginBottom: 12 }} message={gate.reason}
          action={gate.needsCheck ? checkButton('运行一致性检查', { startChapterId: detail.chapter_id, endChapterId: detail.chapter_id }) || undefined : undefined} />}
        {canAssess
          ? (gate.reason
            ? <Button disabled>{assessment ? '重新评估' : '评估影响'}</Button>
            : <Popconfirm title="评估本章的修改影响？" description="最多调用两次模型（使用默认文本模型）。" okText="开始评估" cancelText="取消" onConfirm={onAssess}>
              <Button type="primary" loading={assessing}>{assessment ? '重新评估' : '评估影响'}</Button>
            </Popconfirm>)
          : <Typography.Text type="secondary">当前权限不能发起评估。</Typography.Text>}
      </section>

      <section aria-label="评估结果">
        <Typography.Title level={5}>评估结果</Typography.Title>
        {assessment
          ? <AssessmentView projectId={projectId} detail={detail} assessment={assessment} edits={edits} canConfirm={canConfirm} canEdit={canEdit}
            canStartCheck={canStartCheck} checkBlocked={checkBlocked} saving={saving} onEdits={onEdits} onRequestCheck={onRequestCheck} />
          : <Typography.Text type="secondary">尚未评估。</Typography.Text>}
      </section>
      {!canConfirm && <Typography.Text type="secondary">当前为只读权限，不能修改确认状态。</Typography.Text>}
    </Space></div>}
  </Drawer>
}
