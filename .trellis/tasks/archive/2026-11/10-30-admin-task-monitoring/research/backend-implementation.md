# 后端实施边界与冻结契约

2026-10-31。所有权仅 backend/** 与本研究记录，不改普通 owner 授权。

行为缺口位于 Task 状态条件写、引擎 finalize/recover 与审计提交，不在 HTTP 外套日志。新增三个可空列、独立管理 DTO/service/router、引擎独立 drain；修改 owner 条件写、清理/排空/删号护栏。原子正文保存、统计 UUID、现存历史策略及单进程前提保留；不新增模型调用/历史归档/重试权限。

## API（供前端冻结）

- GET `/api/v1/admin/tasks` query：q(≤200)、owner_user_id/project_id/task_id(正整数)、kind(现有 TaskKind)、重复 status(现有七种 TaskStatus)、成对带时区 from_at/to_at(UTC创建时间半开区间)、sort(created_desc|queue_longest|running_longest)、page=1、page_size=20(≤100)。
- 返回 `{items,total,as_of,counts}`。counts 为七个状态同名计数 + unknown；所有计数属于全部筛选条件内的现存记录，与页码无关。
- GET `/{task_key}` 返回下述 brief 字段加 `as_of`，task_key 路由必须 UUID。
- POST `/{task_key}/cancel` body `{confirm:true,reason}`，trim 后1–200 Unicode字符、拒绝控制字符。202 `{operation_id,audit_log_id:number|null}`，重复 pending 返回相同操作ID，不表示已取消；404原UUID消失，409已结束/状态变化，均固定安全中文detail。

Brief 字段：id:number、task_key:string|null（极旧缺UUID不可处置）、kind:TaskKind|'unknown'、title:固定类型标签+#编号、status:TaskStatus|'unknown'、progress:number、owner_user_id:number、owner_username:string、project_id:number|null、project_title:string|null、project_lifecycle:'active'|'deleting'|'recycled'|'none'、chapter_id:number|null、stage:安全固定文案、error_code:null|'execution_failed'|'cancelled'|'interrupted'|'cleanup_pending'、error_message:string|null、created_at/queued_at/started_at/finished_at:UTC|null（created_at非空）、queued_seconds/running_seconds:number|null、cancellation_pending:boolean、cancel_requested_at:UTC|null、can_cancel:boolean。不返回queue_position。

AuditAction 新增task.cancel_request、target_type新增task。AuditSummary新增task_key:string(UUID)|null、task_kind:TaskKind|'unknown'|null、accepted_from_status:'queued'|'running'|'paused'|null、cancel_reason:string(1–200)|null；仅此动作允许原因自由文本。target_label绝不使用Task.title，用户/作品名称仅作原已许可管理元数据。
