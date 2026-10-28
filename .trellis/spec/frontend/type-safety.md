# 类型安全

> 本项目中的类型安全模式。

---

## 概述

TypeScript ~5.8，启用严格模式（`frontend/tsconfig.app.json`：`strict`、`noUnusedLocals`、`noUnusedParameters`、`noFallthroughCasesInSwitch`、`verbatimModuleSyntax`）。ESLint 强制 `'@typescript-eslint/no-explicit-any': 'error'`。没有运行时校验库（不用 Zod/Yup）：默认信任响应体与 `src/api/types.ts` 一致，而该文件镜像后端的 Pydantic schema。不可信的数据结构（错误响应体、SSE 载荷）通过 `unknown` + 类型守卫进行收窄。

---

## 类型组织

- **所有传输类型都放在 `src/api/types.ts`**（文件头注释：`与后端 app/schemas/*.py 对齐的类型定义`）。每个 Pydantic 模型对应一个 TS interface：`*Out` → 去后缀的名字（`AIConfig`、`Project`、`Chapter`），`*Create`/`*Update`/`*Request` → 同名。
- 字段名保持与传输格式完全一致的 `snake_case`。`datetime` 字段为 `string`。带默认值的可选请求字段用 `?:`；可空字段用 `| null`。
- 枚举是从后端 `Literal[...]` 复制而来的字符串字面量联合类型：

  | TS 类型 | 取值 | 后端来源 |
  |---------|--------|----------------|
  | `ProviderName` | `'openai_compat' \| 'anthropic' \| 'gemini'` | `schemas/ai_config.py` |
  | `ChapterStatus` | `'draft' \| 'generating' \| 'done'` | `schemas/chapter.py` |
  | `ChapterUpdate.status` | `'draft' \| 'done' \| null`（绝不为 `generating`） | `ChapterUpdate.status: Literal["draft", "done"] \| None` |
  | `ProjectStatus` | `'planning' \| 'writing' \| 'completed'` | `schemas/project.py` |
  | `WizardKind` | `'outline' \| 'characters' \| 'world' \| 'careers'` | 向导路由 `/projects/{pid}/wizard/{kind}` |
  | `VersionSource` | `'manual' \| 'baseline' \| 'generate' \| 'polish' \| 'rewrite' \| 'restore' \| 'revise'` | `schemas/chapter_version.py` |
  | `GenerationKind` | `'generate' \| 'polish' \| 'rewrite' \| 'expand' \| 'condense' \| 'deslop' \| 'continue' \| 'revise'` | 同上 |
  | `GenerationStatus` | `'success' \| 'error' \| 'cancelled'` | 同上 |
  | `RewriteOp` | `'rewrite' \| 'expand' \| 'condense' \| 'deslop' \| 'continue'` | 同上 |
  | `TaskKind` | `'chapter.generate' \| 'chapter.polish' \| 'wizard.outline' \| 'wizard.characters' \| 'wizard.world' \| 'character.generate' \| 'organization.generate' \| 'relations.parse' \| 'career.generate' \| 'wizard.careers' \| 'chapter.analyze' \| 'chapter.revise' \| 'outline.continue' \| 'outline.expand' \| 'chapter.batch' \| 'kb.index'` | `schemas/task.py` |
  | `ModelType` | `'text' \| 'image' \| 'embedding'` | `schemas/ai_config.py`（embedding = 向量模型，Anthropic 不支持） |
  | `KbDocumentStatus` | `'pending' \| 'parsing' \| 'indexing' \| 'ready' \| 'ready_keyword_only' \| 'failed'` | `schemas/kb.py`（`parsing` 目前不使用：解析在上传请求内同步完成） |
  | `SearchMode` | `'hybrid' \| 'keyword'` | 同上 |
  | `MemoryKind` / `MemoryOrigin` | `'summary' \| 'plot_point' \| 'hook' \| 'character' \| 'foreshadow' \| 'manual'` / `'auto' \| 'manual'` | `schemas/memory.py` |
  | `OutlineMode` | `'replace' \| 'append'` | `schemas/outline.py`（大纲向导 `outline_mode`、大纲导入 `mode`） |
  | `SnapshotReason` | `'wizard_replace' \| 'import_replace' \| 'reexpand' \| 'manual' \| 'restore'` | 同上 |
  | `OutlineMoveDirection` | `'up' \| 'down'` | 同上 |
  | `AnalysisStatus` | `'none' \| 'analyzing' \| 'analyzed' \| 'stale'` | `schemas/analysis.py` |
  | `ChangeKind` | `'character_state' \| 'life_status' \| 'relationship_update' \| 'relationship_create' \| 'career_stage' \| 'org_membership' \| 'foreshadow_resolve' \| 'foreshadow_plant_planned' \| 'foreshadow_new'` | 同上 |
  | `ForeshadowStatus` | `'pending' \| 'planted' \| 'resolved' \| 'partial' \| 'abandoned'` | `schemas/foreshadow.py` |
  | `ForeshadowSort` | `'planned_resolve' \| 'importance'` | 同上 |
  | `ChangeStatus` | `'applied' \| 'pending' \| 'reverted' \| 'rejected' \| 'skipped' \| 'failed'` | 同上 |
  | `CareerKind` | `'main' \| 'sub'` | `schemas/career.py` |
  | `WorldCategory` | `'place' \| 'faction' \| 'item' \| 'rule' \| 'history' \| 'other'` | `schemas/story.py` |
  | `LifeStatus` | `'alive' \| 'dead' \| 'missing' \| 'other'` | 同上 |
  | `MemberStatus` | `'active' \| 'left' \| 'expelled' \| 'dead'` | `schemas/organization.py` |
  | `RelationCategory` | `'family' \| 'social' \| 'hostile' \| 'work' \| 'other'` | `schemas/relationship.py` |
  | `RelationStatus` | `'normal' \| 'broken' \| 'past' \| 'complicated'` | 同上 |
  | `CardResolution` | `'skip' \| 'rename'` | `schemas/card.py` |
  | `TaskStatus` | `'queued' \| 'running' \| 'paused' \| 'succeeded' \| 'failed' \| 'cancelled' \| 'interrupted'` | 同上 |
  | `InspirationStepKey` | `'title' \| 'synopsis' \| 'theme' \| 'tags' \| 'selling_points' \| 'audience'` | `schemas/inspiration.py`（`StepKey`，顺序即 stale 传播顺序） |
  | `InspirationCurrent` | `'idea' \| InspirationStepKey \| 'scale' \| 'confirm'` | 同上（`CurrentStep`） |
  | `InspirationStepStatus` / `InspirationStepSource` / `InspirationSessionStatus` | `'pending' \| 'decided' \| 'skipped'` / `'ai' \| 'edited' \| 'manual' \| 'project'` / `'active' \| 'converted'` | 同上 |

- UI 标签映射使用 `Record<Union, …>` 类型，因此新增枚举值后在补上标签前会产生编译错误：`PROJECT_STATUS: Record<ProjectStatus, { label; color }>`、`CHAPTER_STATUS: Record<ChapterStatus, …>`、`PROVIDER_LABELS: Record<AIConfig['provider'], string>`、`WIZARD_KIND_LABELS: Record<WizardKind, string>`。
- 类型级映射把泛型参数与载荷绑定：`WizardItemMap { outline: Outline; characters: Character; world: WorldSetting; careers: Career }` → `runWizard<K>(…): Promise<WizardItemMap[K][]>`。
- 组件本地的表单结构（`FormValues`）保留在组件文件中。

- 认证/管理员类型：`User.is_admin` 镜像 `UserOut.is_admin`；`Page<T> = { items: T[]; total: number }` 镜像 `AdminUserList`；`AdminUser` 镜像 `AdminUserOut`（`id, username, is_admin, is_active, created_at, project_count`）；`AdminUserCreate`、`AdminUserUpdate`（`is_admin?`、`is_active?`）、`ResetPasswordRequest`、`ChangePasswordRequest`。后端契约：`.trellis/spec/backend/auth-admin.md`。
- 章节版本 / 生成记录 / 局部改写类型（`10-23-chapter-versions`）：`ChapterVersionBrief`（`id, chapter_id, seq, source, word_count, starred, note, generation_log_id, created_at, updated_at`）、`ChapterVersion`（另加 `content`）、`ChapterVersionUpdate`（`starred?`、`note?` ≤200，省略或 null = 不变）、`GenerationLogBrief`（`params: Record<string, unknown>`、`prompt_tokens`/`completion_tokens` 恒为 null）、`GenerationLog`（另加 `messages: GenerationMessage[]`）、`RewriteRequest`、`RewriteDone`（含 `before`：选区前 ≤200 码点原文）、`RewriteApply`（`before?` 原样回传做乐观校验）、`ContinueEvent`、`ChapterDone`；`GenerateChapterRequest.auto_continue?`（后端默认 false）。标签映射在 `api/chapterVersions.ts`：`VERSION_SOURCE`、`GENERATION_KIND_LABELS`、`GENERATION_STATUS`、`REWRITE_OP_LABELS`（均为 `Record<Union, …>`）。
- 后台任务类型（`10-23-task-engine`）：`Task` 镜像 `TaskOut`（`id, kind, status, title, project_id, chapter_id, progress 0–100, stage, error, retry_of_id, character_id`（取自 `input.request.character_id`，目前仅 `relations.parse` 有值）`, analysis_id`（取自 `input.request.analysis_id`，仅 `chapter.revise` 有值，用于重写前后评分对比）`, queue_position`（仅 queued 有值，0 = 下一个）`, created_at, started_at, finished_at`）；`TaskListQuery`（`project_id? chapter_id? kind? active? limit?`）、`TaskOutput {text}`、`TaskClearResult {deleted}`、`TaskProgress`（订阅 `progress` 载荷，`queue_position?`）。标签映射在 `api/tasks.ts`：`TASK_STATUS: Record<TaskStatus, {label, color}>`、`TASK_KIND_LABELS: Record<TaskKind, string>`；判定函数 `isActiveTask`、`canRetryTask`（`failed|cancelled|interrupted`，与后端 409 规则一致）。`generateChapter` / `polishChapter` / `runWizard` 返回 `Task`；`done.result` 是 `unknown`，必须经 `parseChapterDone` / `parseWizardDone(kind, …)` 收窄（结构不符返回 null，调用方回退为重新拉取 / 空列表）；订阅事件里的 `task` 用 `readTask`（`id: number` + `status: string`）收窄，可能为 null。
- 结构化设定类型（`10-23-story-entities`）：`Character` 新增 `aliases: string[]`、`age`、`gender`、`appearance`、`personality`、`background`、`current_state`、`life_status: string`、`always_in_context`；`CharacterInput.always_in_context?: boolean | null`（创建时省略 = 按定位推断）。`WorldSetting.category: string`（Out 为 str，读取时经 `utils/world.ts` 的 `asWorldCategory` 收窄），`WorldSettingInput.category?: WorldCategory`。组织：`Organization`（含 `member_count`）、`OrganizationInput`、`OrganizationUpdate = Partial<OrganizationInput>`（`parent_id` / `power_level` 显式 null = 清空）、`Member`（含 `organization_name` / `character_name`）、`MemberInput`、`MemberUpdate`、`OrganizationWithMembers(Out)`。关系：`RelationshipType`（`project_id: null` + `builtin` = 预置只读）、`TypeRef`（`type_id` 或 `role_a + role_b + category`）、`RelationshipCreate`、`RelationshipUpdate`（`a_id` 调换方向）、`Relationship`（`other_id/other_name/other_role/self_role` 仅按 `character_id` 查询时有值，否则 null）、`RelationshipHistory.changes: Record<string, unknown>`、`RelationDraft`（`subject_is_a`）、`CharacterWithRelations(Out)`、`RelationshipGraph {nodes, edges, organizations}`、`ChapterCast {character_ids}`。卡片：`CardPreview`、`CardResolutions`、`CardImportResult`。AI 任务结果：`CharacterGenerateResult`、`OrganizationGenerateResult`、`RelationsParseResult`、`RelationSuggestion`、`MemberSuggestion`——`done.result` 必须经 `api/entityTasks.ts` 的 `parseCharacterGenerateResult` / `parseOrganizationGenerateResult` / `parseRelationsParseResult` 收窄（缺字段补默认值，`target_id` 为 null 时强制 `resolved=false`）。标签映射：`api/characters.ts` 的 `LIFE_STATUS`、`ROLE_TYPES`；`api/organizations.ts` 的 `MEMBER_STATUS`；`api/relationships.ts` 的 `RELATION_CATEGORY`、`RELATION_STATUS`（均为 `Record<Union, …>`）；后端返回 `string` 的字段用 `lifeStatusLabel` / `memberStatusLabel` / `categoryLabel` / `statusLabel` 兜底未知值。
- 职业等级体系类型（`10-23-career-system`）：`Career`（`kind: string`，读取时经 `asCareerKind` / `careerKindLabel` 收窄；`stages: CareerStage[]` 按 `position` 从 1 连续；`holder_count`）、`CareerInput`（`stages: StageInput[]` 必填）、`CareerUpdate = Partial<CareerInput>`（给出 `stages` = 整体同步：带 id 更新、无 id 新建、缺失删除，数组顺序即 position）、`CareerHolder`、`Assignment`（镜像 `AssignmentOut`，含 `stage_position / stage_total`，主职业在前）、`AssignmentInput`（`stage_id` 省略 = 第 1 阶，`role` 给出时须等于职业类别）、`AssignmentUpdate`（`chapter_no / history_note` 只记入历史）、`CareerHistory.changes: Record<string, unknown>`（键 `role / stage（「第2阶·筑基」）/ note / removed`）、`CareerAssignDraft`、`CareerGenerateRequest`、`CareerDraft`、`CareerGenerateResult`、`CareerSuggestion`。`CharacterGenerateResult.career_suggestions`（无职业时为空数组）；`CharacterWithRelations.careers?` / `CharacterWithRelationsOut.careers`；`WizardRequest.career_count?`（1–12）；卡片 `CardPreview.careers: CardPreviewCareer[]`（`status: new|reuse`、`stages_added: string[]`）、`CardImportResult.careers: CardImportedCareer[]`（`stages_added: number`）+ `careers_assigned`。`done.result` 收窄：`api/careers.ts` 的 `parseCareerGenerateResult`、`api/entityTasks.ts` 的 `readCareerSuggestions`（`career_id` / `stage_id` 为 null 时强制 `resolved=false`）、`api/wizard.ts` 的 `parseWizardWarnings`（角色 / 职业向导的 `warnings`）。标签：`CAREER_KIND_LABELS`（主修 / 副修）、`CAREER_ROLE_LABELS`（主职业 / 副职业）、`CAREER_SOURCE_LABELS`、`CAREER_HISTORY_FIELD_LABELS`。**阶段提交只用 `rowsToStages`，阶段校验只用 `validateStages`（文案与后端 400 一致），挂接前端预检只用 `assignBlockReason`（文案与后端 409 一致），AI 草稿保存只用 `draftsToBatch`（不含 `conflict`）**——不要在组件里手写。
- 章节分析类型（`10-23-analysis-memory`）：`Project.auto_analyze: boolean` / `ProjectUpdate.auto_analyze?`；`ChapterAnalysisState`（`status, latest_analysis_id, scores, analyzed_at, active_task_id`，均可空）、`AnalysisBrief`（`scores: AnalysisScores`——`Partial<Record<ScoreKey, number \| null>>`，模型没给出的项为 null；`metrics: Partial<AnalysisMetrics>`，`dialogue_ratio` 为 0–1；`stale`）、`Analysis`（+ `summary, plot_points, hooks, conflict, emotion, changes_raw, foreshadow_candidates: unknown[], suggestions, warnings, changes`）、`AnalysisChange`（`target: Record<string, unknown>`、`before` / `after: unknown`——结构随 `kind` 变化且未匹配条目只含原始名字，**只能**经 `api/analysis.ts` 的 `describeChange` 展示，不要在组件里按 kind 解构）、`ReviseChapterRequest`（`suggestion_ids` ≤20、`instruction` ≤2000、`preserve` ≤10、`target_words?` 300–20000；经 `buildReviseRequest` 构造与校验）、`AnalyzeDone`（`chapter.analyze` 的 `done.result`，经 `parseAnalyzeDone` 收窄）。`chapter.revise` 的 `done.result` 与整章生成相同，用 `parseChapterDone`。标签：`ANALYSIS_STATUS`、`SCORE_LABELS`、`CHANGE_KIND_LABELS`、`CHANGE_STATUS`、`SUGGESTION_PRIORITY`（`priorityLabel` 兼容未知值）；`VERSION_SOURCE.revise` / `GENERATION_KIND_LABELS.revise` = `按建议重写`；`TASK_KIND_LABELS` 新增 `章节分析` / `按建议重写`，`CHAPTER_WRITE_KINDS = generate / polish / revise`（`isChapterWriteKind`）。
- 伏笔类型（`10-26-foreshadows`）：`Foreshadow` 镜像 `ForeshadowOut`（四个章节字段 `number \| null` 存章节号；`character_ids` 与 `character_names` 一一对应；派生 `is_long_term`、`overdue_by`（0 = 未超期，列表以项目当前章、提醒以该章为参照））、`ForeshadowInput`（创建）、`ForeshadowUpdate`（省略 = 不变，章节字段显式 null = 清空，`character_ids` 整体替换，`chapter_no` / `history_note` 只记入历史）、`ForeshadowListQuery`、`ForeshadowHistory.changes: Record<string, unknown>`（`{字段: [旧, 新]}`，关联角色以名字列表记在 `characters`）、`ForeshadowStats`（含 `current_chapter`）、`ForeshadowReminders`（五档各 `Foreshadow[]`）、`ForeshadowOverrides`（确认 `foreshadow_new` 用）、`ForeshadowCandidate`（`Analysis.foreshadow_candidates` 仍为 `unknown[]`，只能经 `readCandidates` 收窄）。`analysisApi.confirm(id, overrides?)`：带 overrides 时请求体为 `{overrides}`，否则不带请求体。三个伏笔回写 kind 的「前 → 后」同样只经 `describeChange`（`#id 标题` + 「已埋 · 第 3 章」）。标签：`FORESHADOW_STATUS: Record<ForeshadowStatus, …>`（`foreshadowStatusLabel` 兜底未知值）、`FORESHADOW_FIELD_LABELS`、`FORESHADOW_SOURCE_LABELS`、`CHANGE_KIND_LABELS` 新增 伏笔回收 / 伏笔埋设 / 新伏笔。**表单预检只用 `foreshadowFormError`（文案与后端 400 一致），提交只用 `formToForeshadowInput`，确认表单只用 `newForeshadowDraft` + `buildForeshadowOverrides`，时间线几何只用 `timelineBars` / `timelineRange` / `timelineTicks` / `chapterX`，提醒展示只用 `reminderSections` / `reminderBadgeCount`**——不要在组件里手写。前端 `overdueBy` / `isLongTerm` 与后端派生同口径，仅用于测试与本地判断，列表展示以服务端字段为准。
- 批量生成类型（`10-26-batch-generate`）：`TaskKind` 含 `chapter.batch`，`TaskStatus` 含 `paused`（待审核）；`Task.checkpoint` 的批量分支 `BatchCheckpoint`（联合类型见下方拆书导入条目；首次运行前为 null；`{phase: generate|analyze, current, since_review, items: BatchItem[]}`，`BatchItem.state` = `pending|done|skipped_written|skipped_missing|failed`、`analysis` = `ok|skipped|failed|null`）。判定：`isActiveTask`（queued / running：可订阅、占名额、快速轮询）与 `isOpenTask`（再加 paused：store `active` 列表、「进行中」页签）；`canRetryTask` 不含 paused。`taskApi.resume` / `finish`；`TaskEnd` 多一个 `{status: 'paused', task, result}`，`subscribeTask` 把 `paused` 当终止事件，并分派 `reset`（`useTaskStream` 清空文本）、`stage`、`chapter_start`（`isBatchChapterStart`）、`chapter_end`（`isBatchChapterEnd`）；`useTaskStream().follow(task, extra?)` 透传 `onSnapshot / onChapterStart / onChapterEnd`。store：`upsertActive` 按 `isOpenTask` 保留；轮询节奏按 `countRunning`（待审核不触发 3 秒轮询）；`countFinished` 把「活动 → 待审核」也算一次变化（`version` 递增）。`BatchGenerateRequest` / `BatchPreview`（`items` / `skipped` / `analyze` / `estimated_minutes` / `warnings`）镜像 `schemas/batch.py`，接口 `previewBatch` / `startBatch` 在 `api/chapters.ts`；`done` / `paused` 的 result 只能经 `utils/batch.ts` 的 `parseBatchSummary` 收窄。`Task.step_cursor: number` 镜像 `TaskOut.step_cursor`（断点：下一章在 `checkpoint.items` 中的下标；失败停下时停在失败章、分析阶段中断时停在待分析章、`skip_on_fail` 跳过后已越过）。续跑章节号只用 `utils/batchState.ts` 的 `batchResumeIndex`（= `step_cursor`，没有断点或已越界时为 null），**不要**解析 `error` 文案推导位置。
- 拆书导入类型（`10-23-book-import`）：`BookImport` / `BookImportBrief` / `BookChapterView`（`volume_no` 0 = 不属于任何卷，无分卷时全部为 1）/ `BookOp` / `BookRange = [number, number]`（从 1 起、两端包含）/ `BookEstimate` / `BookStartResult {project_id, task}` 镜像 `schemas/book_import.py`，接口只在 `api/bookImport.ts`（拆书页懒加载使用）；「跳过本批」是 `taskApi.skipBookImport`（任务面板在入口 chunk，不能引用 `api/bookImport.ts`）。`TaskKind` 含 `book.import`；`VersionSource` 含 `import`；`GenerationKind` 含 `book_import.digest | book_import.synthesize | book_import.style`。
  **`Task.checkpoint` 按任务类型区分**：类型为 `BatchCheckpoint | BookImportSummary | null`（其他类型恒为 null；`book.import` 是后端 `checkpoint_out` 给的断点摘要 `{phase, batches_total, batches_done, batches_skipped, stats, skipped, warnings, pause_reason}`，不含 findings）。**读取时必须先按 kind 收窄，不能直接访问 `.items` / `.pause_reason`**：批量用 `utils/batchState.ts` 的 `batchCheckpoint(checkpoint)`（有 `items` 数组才返回）；拆书在入口 chunk 用 `utils/bookImportState.ts` 的 `bookImportPause` / `bookImportBatches` / `canSkipBookImport`，完整摘要用 `utils/bookImport.ts` 的 `bookImportSummary(value)`（`done.result` / `paused` 载荷 / `checkpoint` 同一结构，缺省字段补默认值，结构不符为 null）。新增多步任务类型时扩展这个联合并配一个收窄函数。
- 项目智能体类型（`10-23-project-agent`）：`AgentConversation` / `AgentConversationDetail`（`messages` / `changes` / `active_task`）/ `AgentMessage`
  （`tool_calls: AgentToolCall[] | null`，只有 `id / name / arguments`，后端已去掉 `provider_metadata`）/ `AgentChange`（`kind: small | delete | task | text`、
  `status: AgentChangeStatus` = `proposed | executing | executed | rejected | failed | undone`、`preview: AgentPreview {title, target, diff: [{field, label, before, after}], warnings}`、
  `effects: AgentEffect[]`）/ `AgentPageContext` / `AgentSendRequest` / `AgentSendResult` / `AgentTurnResult {changes, task, error?}` / `AgentToolStart` / `AgentToolEnd`
  镜像 `schemas/agent.py`，接口只在 `api/agent.ts`。`TaskKind` 与 `GenerationKind` 含 `agent.turn`，`VersionSource` 含 `agent`（标签「智能体修改」）。
  `Task.checkpoint` 的联合增加 `AgentCheckpoint {tool_calls_used, pending_change_ids, assistant_message_id}`（只镜像前端会读的字段，后端断点还有其他键）。
  任务流的自定义事件 `tool_start` / `tool_end` / `proposal` / `executed` 只能经 `api/agentEvents.ts` 的 `isAgentToolStart` / `isAgentToolEnd` / `readAgentChange` 收窄后再分派
  （`subscribeTask` → `useTaskStream().follow(task, {onToolStart, onToolEnd, onProposal, onExecuted})`），结构不符的载荷直接丢弃。
  `preview.target` / `diff[].before / after` 是 `unknown`、`result` 是 `Record<string, unknown> | null`，显示一律经 `utils/agent.ts` 的 `agentValue`；取任务 id 用 `changeTaskId`，不要直接访问 `result.task.id`。
  错误标记：修订号过期的 409 响应体带 `code: 'stale_revision'`，读 `ApiError.body?.code`（`isStaleRevision`），不匹配 `detail` 文案。`batch_end` 事件经 `isBookBatchEnd` 校验后分派给 `onBatchEnd`（`useTaskStream().follow(task, extra)` 透传）。
- 提示词模板与写作风格类型（`10-23-prompt-styles`）：`StyleChoice = 'inherit' | 'none' | number`，`GenerateChapterRequest` / `PolishChapterRequest` / `ReviseChapterRequest` / `RewriteRequest` / `BatchGenerateRequest` 的 `style?`（后端默认 inherit）。`Project` 去掉 `style`，新增 `default_style_id: number \| null`、`default_style: StyleBrief \| null`、`notice?`；`ProjectCreate` / `ProjectUpdate` 用 `default_style_id`（显式 null = 取消），**不再发送已废弃的 `style`**。`ChapterBrief.style_id: number \| null`，`ChapterUpdate.style_id?`（显式 null = 继承项目默认）。风格：`StyleFields`（description ≤200、6 个维度 ≤500、taboos ≤1000、sample ≤2000、extra ≤5000）、`WritingStyle`（`is_preset`、`preset_key`、`references {projects, chapters}`）、`StyleBrief`、`StyleCreate` / `StyleUpdate`（省略 = 不变）、`StyleDraft`（名称可空）、`StyleExtractRequest` / `StyleExtractDone {draft, warnings}`（经 `parseExtractDone` 收窄）、`StyleTrialRequest`（`style_id` 与 `style` 二选一）/ `StyleTrialDone`、`StyleImportResult`。模板：`TemplatePart`、`TemplateIssue {part, line, message}`、`TemplateVar`（`kind: string`）、`LockedBlock`、`PromptTemplateBrief`（`overridden / enabled / outdated`）、`PromptOverride`（`base_system / base_user` = 保存时的旧默认）、`PromptTemplateDetail`、`PromptSave`、`PromptPatch`、`PromptVersion`、`PromptPreviewRequest` / `PromptPreview`、`PromptImportStatus` / `PromptImportResult`。纯函数：`utils/styles.ts`（`resolveStyleChoice` 与后端优先级一致、`describeInherit`、`styleChoiceOptions`、`styleAssignOptions`、`referencesText`、`readStyleReferences`、`summarizeStyleImport`、`draftToStyleBody`）、`utils/prompts.ts`（`formatIssue` 与后端 `format_issue` 同口径、`issuesFromError`、`issuesForPart`、`lineRange`、`groupTemplates`、`templateBadges`、`splitLocked`、`summarizePromptImport`、`PROMPT_IMPORT_STATUS: Record<PromptImportStatus, …>`）、`utils/jsonFile.ts`（`parseJsonFileText` 紧凑 UTF-8 口径、`datedFilename`）。用户级 store `stores/styles.ts`（`useStyleStore` / `useStyles()`）在 `App.tsx` 随 `userId` 重置。
- 大纲进阶类型（`10-23-outline-pro`）：`ChapterBrief.plan_id: number | null`（按细纲建章的章节）；`Project.wizard_progress: WizardProgress {done: string[], skipped: string[], current: string | null}` 与 `WizardProgressUpdate`（给出的字段整体替换，`current` 显式 null = 清空；步骤名由前端定义——`world / careers / characters / outline / finish`，后端不校验）；`WizardRequest.outline_mode?: OutlineMode`（默认 replace），大纲向导 `done.result.mode` 经 `parseWizardMode` 读取；`Task.outline_id`（取自 `input.request.outline_id`，仅 `outline.expand` 有值，用于按大纲接上活动任务）。`Plan` 镜像 `PlanOut`（`seq` 从 1 起、`character_ids` 已过滤被删角色、`chapter_id / chapter_no` 已建章时有值、`has_content` = 已建章且有正文 → 不能删除）、`PlanFields` / `PlanInput`（`id?` = 更新）/ `PlansSync`（PUT 整体同步：缺失 = 删除，顺序即 seq）/ `PlanUpdate`（PATCH 省略 = 不变）、`PlanBuildResult`、`OutlineDetail`（`Outline` + `chapter_id`（未展开时的 1 纲 1 章）+ `plans` + `plan_count` + `built_count`）、`OutlineAppendItem`、`OutlineSnapshot` / `OutlineSnapshotDetail`（+ `outlines: SnapshotOutlineBrief[]`、`warnings`）/ `SnapshotRestoreResult`（`backup` = 恢复前自动保存的快照）、`OutlineImportPreview` / `OutlineImportResult`（`snapshot_id` 仅 replace）、`OutlineContinueRequest`（`new_volume?: {title} | null`）/ `OutlineExpandRequest`（`count` = 总章数含保留）、`OutlineContinueResult` / `OutlineExpandResult`（`drafts[].characters: {name, character_id, resolved}[]`、`existing[]`）。`done.result` 只能经 `api/outlines.ts` 的 `parseContinueResult` / `parseExpandResult` 收窄（`character_id` 为 null 时强制 `resolved=false`）。**PUT 细纲时保留项必须用 `planToInput` 带全字段**（`PlanIn` 缺省字段会被重置为默认值，`existing` 不含关键事件等字段，所以展开预览以重新拉取的 `GET /plans` 为准）；展开采纳只用 `buildExpandSync(keptPlans(plans), drafts)`、删除单条只用 `removePlanSync`、续写采纳只用 `continueAppendItems`（章节号由后端顺延，预览号 `continuePreviewNos`）、文件解析只用 `parseOutlineFileText`（2MB 紧凑序列化口径，与卡片一致）——不要在组件里手写。标签：`SNAPSHOT_REASON_LABELS: Record<SnapshotReason, …>`、`OUTLINE_MODE_LABELS`、`TASK_KIND_LABELS` 新增 AI 续写大纲 / AI 展开细纲；展开状态文案只用 `expandStatus`、章节号范围只用 `chapterRange`。
- 灵感模式与立项结构化字段类型（`10-23-inspiration`）：`Project` 新增 `synopsis / theme / tags: string[] / selling_points / audience: string`、`target_words: number | null`；`ProjectCreate` / `ProjectUpdate` 同名可选字段（`target_words` 显式 null = 清空）；`WizardProgress.params?: WizardParams | null`（`{volume_count, chapters_per_volume, character_count}`，只由「转为项目」写入，旧数据 / 测试字面量可省略）。灵感会话：`InspirationSession`（`SessionOut`，`state: InspirationState {idea, notes, genre, steps: Record<InspirationStepKey, InspirationStepState>, scale: InspirationScale | null, current}`；`InspirationStepState.value` 在 tags 步骤为 `string[]`、其余 `string`、未定 / 跳过为 null；`rounds: InspirationRound[]` 最多 10 轮）、`InspirationSessionBrief`（列表，`done_steps / total_steps / stale_steps / current`）、`InspirationSessionUpdate`（`scale` 显式 null = 清空）、`InspirationStepUpdate {action: decide|skip|keep|clear, value?, source?: ai|edited|manual}`（`project` 来源只由后端写入）、`InspirationGenerateRequest`、`InspirationGenerateDone {round, session}`（`parseGenerateDone` 收窄）、`InspirationRetryEvent {attempt, max, reason}`（`parseRetry`）、`InspirationConvertRequest`、`InspirationApplyField`（6 个步骤 + `premise` + `target_words`）、`InspirationApplyPreviewItem {field, label, current, proposed, default_checked}`、`InspirationApplyRequest`。标签 `Record`：`FLOW_LABELS: Record<InspirationCurrent, string>`、`APPLY_FIELD_LABELS`、`SESSION_STATUS`、`STEP_SOURCE_LABELS`、`STEP_HINTS`（`utils/inspiration.ts`）。
- 阅读器类型（`10-27-reader-ui`）：`Project` 新增 `last_edited_chapter_id: number | null`（正文非空、`updated_at` 最大的章节；没有已写章节时 null）与 `reading: ReadingPosition | null`（`{chapter_id, chapter_no, ratio}`，ratio 0–1；进度章被删除时后端退回之前最近的已写章节且 ratio = 0）；`ReadingProgressUpdate {chapter_id, ratio}`（`PUT /projects/{pid}/reading-progress` → 204；章节不属于项目 404、ratio 越界 422；写进度不改 `project.updated_at`）；`ReaderTocItem {id, chapter_no, title, word_count, volume_no: number | null, volume_title: string | null}`（`GET /projects/{pid}/reader/toc`，只含 `word_count > 0` 的章节，没有关联大纲时卷字段为 null）。接口在 `api/reader.ts`：`readerApi.toc / saveProgress`，以及卸载时用的 `saveProgressKeepalive`（`fetch(buildUrl(...), { method: 'PUT', keepalive: true, credentials: 'include' })`，失败静默——这是 `src/api/` 内除 `client.ts` / `downloadAttachment` 外唯一直接调用 `fetch` 的地方）。测试里构造 `Project` 字面量时要带上这两个字段（见 `utils/shelf.test.ts`）。
- 知识库与剧情记忆类型（`10-23-knowledge-base`）：`KbSettings`（`auto_inject, top_k, min_score, max_chars, memory_top_k, memory_max_chars, memory_min_score`）与 `KbSettingsUpdate = Partial<KbSettings>`（省略 = 不变）；`KbDocument`（含 `warnings: string[]`、`error`、`char_count`、`chunk_count`、`inject`）、`KbUploadResult {document, task}`、`KbChunk` / `KbChunkPage`、`KbSearchRequest`（`top_k? / min_score?` 省略或 null = 按项目设置）、`KbSearchHit`（`vector_score: number \| null`——只用关键词时为 null）、`KbSearchResult {hits, mode, warnings, candidates}`、`IndexStatus {embedding_model: string \| null, needs_rebuild, stale_count, stale: {kb, memory}, indexing}`。记忆：`Memory`（`chapter_id` 可空：删章后手动记忆保留）、`MemoryPage`、`MemoryListQuery`、`MemoryCreate`、`MemoryUpdate`（省略 = 不变；`chapter_no / content / characters / importance` 只有手动记忆可改，自动记忆改内容 → 400）、`MemorySearchRequest`（`before_chapter_no?`、`include_muted?`）、`MemorySearchHit {memory, score, vector_score, keyword_score}`、`MemorySearchResult`、`ContextPreview {knowledge, memories}`。重复上传 409 的 `document_id` 只经 `api/kb.ts` 的 `duplicateDocumentId(err)` 从 `ApiError.body` 收窄读取。
- **关系语义**：「a 是 b 的 `role_a`，b 是 a 的 `role_b`」。前端视角换算只用 `perspective(rel, viewerId)` / `typeOptions` / `describeRelation`；保存建议只用 `suggestionsToDrafts`（with-relations）/ `suggestionsToBatch(subjectId, …)`（`a_id = subject_is_a ? 主体 : 目标`）——不要在组件里手写方向判断。
- **偏移与字数按码点**：后端偏移与 `len()` 都按 Unicode 码点，JS 字符串按 UTF-16。TextArea 偏移发请求前用 `utils/textOffset.ts` 的 `utf16ToCodePoint` 换算，回显用 `codePointToUtf16`；选区长度用 `codePointLength`。`countWords` 为 `Array.from(text.replace(/\s+/g, '')).length`（emoji / 扩展 B 区生僻字算 1 字），与后端 `count_words` 一致。`textOffset.test.ts` 覆盖 emoji 与 `𠀀`/`𠮷`。
- 共享表单规则放在 `src/utils/validation.ts`（`usernameRules` 3–32 位且不含空白、`passwordRules` 6–72 位、`confirmPasswordRules(field = 'password')`）。登录/注册、修改密码、管理员创建用户和重置密码都导入它们——绝不在页面中重复声明这些限制。

### PATCH 语义（必须与后端 service 一致）

| 资源 | 规则 | 后端依据 |
|----------|------|------------------|
| 项目、世界观/角色/大纲 | 省略**或** `null` = 不变 | `model_dump(exclude_unset=True)` + `if value is None: continue`（`project_service.update_project`、`story_service.update_owned`） |
| 章节 | 对 `chapter_no`、`title`、`content`、`status` 而言省略/`null` = 不变；显式发送 `outline_id: null` 会**解除**与大纲的关联 | `chapter_service.update_chapter` 检查 `"outline_id" in fields` |
| `generating` 中的章节 | PATCH 带 `title`/`content` 且不带 `status` → 409；仅发送 `{ status: 'draft' }` 是复位路径 | `ConflictError("该章节正在生成中，请等待完成或先复位状态后再编辑")` |
| 管理员用户 | 省略 `is_admin` / `is_active` = 不变；禁用会递增 `token_version`（目标用户的会话 → 401） | `admin_service.update_user` |
| AI 配置 | 省略 `api_key` 或传 `''` 保留已存储的密钥；只要出现 `base_url`（包括 `null`/`''`）就会被应用，`null` = provider 默认值 | `ai_config_service.update_config` 中的 `if data.api_key:` / `if "base_url" in fields:` |

前端的相应做法（`AIConfigPage.handleSave`）：始终发送 `base_url`（`v.base_url?.trim() ? v.base_url.trim() : null`），`api_key` 仅在非空时发送。`AIConfigUpdate = Partial<AIConfigCreate>`。

### 时间戳

后端的 datetime 序列化时可能不带时区后缀。务必通过 `src/utils/time.ts` 解析：

```ts
export function parseServerTime(value: string): dayjs.Dayjs {
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(value)
  return dayjs(hasZone ? value : `${value}Z`)   // 无时区 -> UTC
}
export function formatTime(value: string | null | undefined, fmt = 'YYYY-MM-DD HH:mm'): string
```
`time.test.ts` 断言 `'2026-10-22T08:00:00'` → `'2026-10-22T08:00:00.000Z'`，并断言 `+08:00` / `Z` 输入保留其时区。绝不要直接对服务端时间戳调用 `dayjs(value)` 或 `new Date(value)`。

---

## 校验

- **输入**：AntD `Form` 规则与后端 `Field` 限制保持一致（例如向导中 `volume_count` 为 `InputNumber min={1} max={20}`，`character_count` 为 `min={3} max={30}`，编辑器 `target_words` 为 `min={300} max={20000}`，指令 `max: 2000`；登录用户名 3–32 位，密码 6–72 位）。字符串发送前先 `.trim()`。后端仍是最终权威，并返回 `{detail}`（422 的 detail 以 `请求参数校验失败：` 开头）。
- **响应**：不做运行时校验；通过 `request<T>` 定类型。
- **不可信 JSON**：从 `unknown` 收窄：
  ```ts
  // src/api/client.ts
  function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null
  }
  const data: unknown = JSON.parse(text)
  if (isRecord(data) && typeof data.detail === 'string') return data.detail
  ```
  `sse.ts` 的 `readText`/`readDetail` 对 SSE 载荷做同样处理；`runGeneration` 接受一个可选的 `parseDone?: (data: unknown) => TDone` 校验函数；`subscribeTask` 用 `isContinueEvent` / `isProgress` / `readTask` 守卫各事件载荷。

---

## 常用模式

- 全部使用 `catch (err)` / `(err: unknown)`，并通过 `errorMessage(err)` 转换。
- 结果使用可辨识联合类型：`StreamOutcome<T>`（`status: 'done' | 'aborted' | 'error'`）、`AuthStatus`。
- 泛型 API 辅助函数：`request<T>`、`api.get<T>`、`makeCrud<TItem, TInput>`、`runGeneration<TDone>`。
- 仅类型导入使用 `import type { … }`（`verbatimModuleSyntax` 要求）。

---

## 场景：API 客户端契约

### 1. 范围 / 触发条件
任何对 `src/api/client.ts`、`src/api/projects.ts` 中 `exportProject` / `downloadAttachment` / `parseContentDispositionFilename` / `decodeExportWarnings`、`src/api/transfer.ts`、后端错误格式（`backend/app/core/errors.py`）、认证 cookie 或导出端点（`GET /api/v1/projects/{id}/export`）的修改。

### 2. 签名
```ts
export const API_BASE = '/api/v1'
export class ApiError extends Error { readonly status: number; readonly detail: string; readonly body: Record<string, unknown> | null }
export function setUnauthorizedHandler(handler: (() => void) | null): void
export function buildUrl(path: string, query?: Record<string, QueryValue>): string
export async function readErrorBody(resp: Response): Promise<{ detail: string; body: Record<string, unknown> | null }>
export async function readErrorDetail(resp: Response): Promise<string>
export async function handleErrorResponse(path: string, resp: Response): Promise<never>
export async function rawRequest(path: string, options?: RequestOptions): Promise<Response>
export async function request<T>(path: string, options?: RequestOptions): Promise<T>
export const api: { get<T>(path, query?); post<T>(path, body?, signal?); patch<T>(path, body); put<T>(path, body); del(path, query?): Promise<void>; upload<T>(path, form: FormData, signal?) }
export function errorMessage(err: unknown): string
export function parseContentDispositionFilename(header: string | null): string | null
export type ExportFormat = 'txt' | 'md' | 'epub'
export function decodeExportWarnings(header: string | null): string[]   // decodeURIComponent 后按「；」切分，非法编码按原文
export async function downloadAttachment(path, query, fallbackName): Promise<{ filename: string; headers: Headers }>
export function exportQuery(format, { chapterIds?, author?, includeSynopsis? }): Record<string, string>   // EPUB 才带 author / include_synopsis
export async function exportProject(projectId, format, options?: ExportOptions): Promise<{ filename: string; warnings: string[] }>
// src/api/transfer.ts（只被懒加载的备份 / 导入弹窗引用）
export function backupQuery(options: BackupOptions): Record<string, boolean>   // generation_logs → logs
export const transferApi: { estimate(pid); download(pid, options): Promise<string>; preview(file, signal?): Promise<ImportPreview>; importBackup({token, title?}): Promise<ImportResult> }
```

### 3. 契约
- 每个请求都使用 `credentials: 'include'`（HttpOnly cookie 会话；JS 中不持有 token）。请求头：`Accept: application/json`；仅在有 JSON 请求体时设置 `Content-Type: application/json`；请求体为 `FormData`（`api.upload`，知识库上传）时原样发送、**不设置** `Content-Type`（由浏览器带 multipart boundary）。
- `buildUrl` 跳过值为 `null`/`undefined` 的查询参数，数组则重复同一个 key。
- 非 2xx → `handleErrorResponse`：读取响应体文本，若 `detail` 为字符串则提取它（后端总是发送 `{"detail": "中文"}`），否则回退为 `请求失败（HTTP ${status}）`；响应体是 JSON 对象时整体保存在 `ApiError.body`（后端 `AppError.extra` 的结构化字段：模板校验 400 的 `errors`、删除被引用风格 409 的 `references`），只能经 `utils/prompts.ts` 的 `issuesFromError` / `utils/styles.ts` 的 `readStyleReferences` 收窄读取，非 JSON 时为 null；遇到 401 时调用已注册的处理器，除非路径以 `/auth/login` 或 `/auth/me` 开头；始终抛出 `ApiError(status, detail)`。
- 网络失败 → `ApiError(0, '网络错误，无法连接服务器')`；`AbortError` 原样重新抛出。
- `request<T>`：204 → `undefined`；空响应体 → `undefined`；否则 `JSON.parse`。
- `App.tsx` 注册 `() => useAuthStore.getState().clear()`；随后 `RequireAuth` 重定向到 `/login`。
- 导出 / 备份下载：`downloadAttachment` 直接调用 `fetch(buildUrl(path, query), { credentials: 'include' })`（导出 query = `exportQuery`：`{ format, chapter_ids?, author?, include_synopsis? }`；备份 query = `backupQuery`），其中 `chapter_ids` 是逗号拼接的字符串（后端 `export_service.parse_chapter_ids`）。响应为 blob；文件名取自 `Content-Disposition`，优先使用 RFC 5987 的 `filename*=UTF-8''<percent-encoded>`（后端发送 `attachment; filename="novel.{ext}"; filename*=UTF-8''{quote(name)}`），其次回退到普通 `filename=`，最后为 `novel.${format}`。通过 object URL + 临时 `<a download>` 下载，1 秒后撤销 URL。错误同样经过 `handleErrorResponse`。`X-Export-Warnings`（EPUB 跳过的空章节等）是 UTF-8 百分号编码、多条以「；」分隔，后端 CORS 已 `expose_headers`；`exportProject` 用 `decodeExportWarnings` 解码后返回 `warnings`。
- 备份类型（`types.ts`）：`BackupOptions` / `BackupEstimate` / `ImportPreview` / `ImportStyleBrief`（`action: preset|reuse|create|copy`）/ `ImportRequest` / `ImportResult` 镜像 `backend/app/schemas/transfer.py`。预览成功体**没有** `errors`：文件不合法时为 400，问题列表在 `ApiError.body.errors`，只能经 `utils/transfer.ts` 的 `importFileErrors` 收窄读取。导入用 JSON `{token, title?}`（后端也接受 multipart 直接导入，前端不用）。

### 4. 校验与错误矩阵

| 输入 | 结果 |
|-------|--------|
| 2xx JSON | 解析后的 `T` |
| 204 / 空响应体 | `undefined` |
| 4xx/5xx 且为 `{"detail":"…"}` | `ApiError(status, detail)` |
| 4xx/5xx 且非 JSON 或 `detail` 非字符串 | `ApiError(status, '请求失败（HTTP n）')` |
| 除 `/auth/login*`、`/auth/me*` 外任意路径的 401 | 处理器 → `clear()` → 重定向到登录页；仍然抛出 `ApiError(401, …)` |
| `/auth/login`（密码错误）或 `/auth/me`（未登录）的 401 | 仅抛出；`LoginPage` 显示 detail，`init()` 设为匿名 |
| `fetch` reject | `ApiError(0, '网络错误，无法连接服务器')` |
| signal 已中止 | 重新抛出 `DOMException` `AbortError` |
| 导出 `filename*=UTF-8''%E5%B0%8F%E8%AF%B4.txt` | `小说.txt` |
| 导出响应头的百分号编码格式错误 | 回退到普通 `filename` |
| 导出响应头缺失 | `novel.txt` / `novel.md` |

### 5. 正例 / 基准 / 反例
- 正例：`await chapterApi.update(pid, cid, { title, content })` 解析为 `Chapter`；失败时 `message.error(errorMessage(err))` 显示后端的中文 detail。
- 基准：`api.del('/projects/1')` → 204 → 解析为 `undefined`。
- 反例：对生成中的章节 PATCH content → `ApiError(409, '该章节正在生成中，请等待完成或先复位状态后再编辑')`；任何会话过期 → 全局登出，而非各页面单独处理。

### 6. 必需的测试
`src/api/client.test.ts`（`10-23-knowledge-base` 新增）覆盖 `client.ts` 的前四条与 FormData 上传；`src/api/projects.test.ts`（`10-23-project-io-reader` 新增）覆盖 `parseContentDispositionFilename`、`decodeExportWarnings`、`exportQuery`、`backupQuery`（下载本身仍没有单元测试）。修改它们时，需添加 / 更新 Vitest 测试（stub `globalThis.fetch`），断言：
- 调用 `fetch` 时带有 `credentials: 'include'`，且 URL 以 `/api/v1` 为前缀。
- 400 且错误体为 `{"detail":"x"}` → `ApiError` 满足 `status === 400`、`detail === 'x'`；非 JSON 响应体 → 回退 detail。
- `/projects` 返回 401 时调用已注册处理器一次；`/auth/login` 和 `/auth/me` 返回 401 时不调用。
- 204 解析为 `undefined`。
- `parseContentDispositionFilename`：`filename*=UTF-8''%E5%B0%8F%E8%AF%B4.txt` → `小说.txt`；仅有 `filename="a.md"` → `a.md`；`null` → `null`。

### 7. 错误写法 vs 正确写法
```ts
// 错误：绕过了 cookie 会话、错误归一化和 401 hook
const r = await fetch('/api/v1/projects'); const data = await r.json()

// 错误：显示 "[object Object]" 或英文 HTTP 文本，而不是后端的 detail
message.error(String(err))

// 错误：先读取 ASCII 回退值，导致中文标题总是被下载为 novel.txt
/filename="?([^";]+)"?/.exec(header)

// 正确
const projects = await projectApi.list()
message.error(errorMessage(err))
parseContentDispositionFilename(resp.headers.get('content-disposition')) ?? `novel.${format}`
```

---

## 禁止模式

- `any`（lint 报错）。使用 `unknown` + 类型收窄。
- 在 `src/api/` 之外直接调用 `fetch`——应通过 `api.*`、`postSSE`/`runGeneration` 或 `exportProject`。
- 在组件内重新声明传输类型；应扩展 `types.ts`。
- 在 `ChapterUpdate` 中发送 `status: 'generating'`（类型禁止；后端拒绝）。
- 不通过 `parseServerTime`/`formatTime` 解析服务端时间戳。
- 对服务端数据做宽泛的 `as` 断言。在类型系统无法推导的地方，以下现有的窄断言是可接受的：`request` 中的 `undefined as T` / `JSON.parse(text) as T`、`WizardGenerator` 中在 `kind` 检查后的 `result as WizardItemMap['world'][]`、来自固定单选组的 `e.target.value as ExportFormat`、`utils/transfer.ts` 中对后端 `action: str` 的 `action as ImportStyleAction`（查表失败有回退），以及 `main.tsx` 中的非空断言 `document.getElementById('root')!`。
