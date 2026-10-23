/** 任务结果查看组件的统一 props（任务面板注册表与生成弹窗共用） */
export interface TaskResultViewerProps {
  projectId: number
  /** 任务 done.result（unknown，由组件自行收窄） */
  result: unknown
  /** 保存成功后回调（刷新列表等） */
  onSaved?: () => void
}
