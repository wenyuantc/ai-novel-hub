// 与后端 app/schemas/*.py 对齐的类型定义

export interface User {
  id: number
  username: string
  is_admin: boolean
  created_at: string
}

export interface AuthConfig {
  registration_enabled: boolean
}

export interface Credentials {
  username: string
  password: string
}

export interface ChangePasswordRequest {
  old_password: string
  new_password: string
}

/** 分页结果（后端 {items, total}） */
export interface Page<T> {
  items: T[]
  total: number
}

/** 管理员视角的用户（不含任何小说内容） */
export interface AdminUser {
  id: number
  username: string
  is_admin: boolean
  is_active: boolean
  created_at: string
  project_count: number
}

export interface AdminUserListQuery {
  q?: string
  page?: number
  page_size?: number
}

export interface AdminUserCreate {
  username: string
  password: string
  is_admin?: boolean
}

/** 省略的字段保持不变 */
export interface AdminUserUpdate {
  is_admin?: boolean
  is_active?: boolean
}

export interface ResetPasswordRequest {
  new_password: string
}

export type ProviderName = 'openai_compat' | 'anthropic' | 'gemini'

/** off = 不发送思考参数 */
/** embedding = 向量模型（知识库与剧情记忆的语义检索；Anthropic 不支持） */
export type ModelType = 'text' | 'image' | 'embedding'

export type ThinkingLevel = 'off' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'

export interface AIConfig {
  id: number
  name: string
  provider: ProviderName
  base_url: string | null
  model: string
  model_type: ModelType
  api_key_masked: string
  is_default: boolean
  thinking_level: ThinkingLevel
  /** 单位 K（×1000 tokens），null = 默认 */
  context_window_k: number | null
  max_output_k: number | null
  created_at: string
  updated_at: string
}

export interface AIConfigCreate {
  name: string
  provider: ProviderName
  base_url?: string | null
  api_key?: string
  model: string
  model_type?: ModelType
  is_default?: boolean
  thinking_level?: ThinkingLevel
  context_window_k?: number | null
  max_output_k?: number | null
}

export type AIConfigUpdate = Partial<AIConfigCreate>

export interface ModelListRequest {
  provider: ProviderName
  base_url?: string | null
  /** 为空时用 config_id 对应配置已保存的 Key */
  api_key?: string | null
  config_id?: number | null
}

export interface ModelInfo {
  id: string
  name: string | null
}

export interface AIConfigTestResult {
  ok: boolean
  message: string
  latency_ms: number
}

export type ProjectStatus = 'planning' | 'writing' | 'completed'

export interface Project {
  id: number
  title: string
  genre: string
  premise: string
  status: ProjectStatus
  created_at: string
  updated_at: string
  chapter_count: number
  total_words: number
  /** AI 生成 / 润色 / 按建议重写成功后自动排队分析本章（默认 true） */
  auto_analyze: boolean
  /** 向导进度（outline-pro）：步骤名由前端定义（WIZARD_STEP_KEYS），后端不校验取值 */
  wizard_progress: WizardProgress
  /** 项目默认写作风格（prompt-styles）；null = 不用风格 */
  default_style_id: number | null
  default_style: StyleBrief | null
  /** 兼容提示（例如请求里仍传了已废弃的 style 字段） */
  notice?: string | null
  /** 立项结构化字段（inspiration）：非空时注入所有 AI 调用的【作品信息】；target_words 不进提示词 */
  synopsis: string
  theme: string
  /** 类型标签：≤8 个、每个 ≤10 字（后端 normalize_tags 去空白、去重） */
  tags: string[]
  selling_points: string
  audience: string
  /** 目标总字数（1000–10,000,000）；null = 未设置 */
  target_words: number | null
  /** 最近编辑的章节（reader-ui）：正文非空、updated_at 最大；没有已写章节时为 null */
  last_edited_chapter_id: number | null
  /** 阅读进度（reader-ui）；从未阅读为 null。进度指向的章节已删除时后端退回之前最近的已写章节 */
  reading: ReadingPosition | null
}

/** ProjectOut.reading */
export interface ReadingPosition {
  chapter_id: number
  chapter_no: number
  /** 章内位置 0–1 */
  ratio: number
}

/** PUT /projects/{pid}/reading-progress（204） */
export interface ReadingProgressUpdate {
  chapter_id: number
  ratio: number
}

/** GET /projects/{pid}/reader/toc：只含已写章节，按章节号升序；卷信息经 outline_id 取大纲 */
export interface ReaderTocItem {
  id: number
  chapter_no: number
  title: string
  word_count: number
  /** 没有关联大纲的章节两者都为 null */
  volume_no: number | null
  volume_title: string | null
}

/** 向导参数建议（灵感模式转为项目时写入；向导页优先用它初始化参数） */
export interface WizardParams {
  volume_count: number
  chapters_per_volume: number
  character_count: number
}

export interface WizardProgress {
  done: string[]
  skipped: string[]
  current: string | null
  /** 只由灵感模式「转为项目」写入；PATCH /wizard-progress 不修改它 */
  params?: WizardParams | null
}

/** PATCH /projects/{pid}/wizard-progress：给出的字段整体替换，未给的不变；current 显式 null = 清空 */
export interface WizardProgressUpdate {
  done?: string[]
  skipped?: string[]
  current?: string | null
}

export interface ProjectCreate {
  title: string
  genre?: string
  premise?: string
  /** 项目默认写作风格；已废弃的 style 自由文本前端不再发送 */
  default_style_id?: number | null
  synopsis?: string
  theme?: string
  tags?: string[]
  selling_points?: string
  audience?: string
  /** PATCH 时显式 null = 清空 */
  target_words?: number | null
}

export interface ProjectUpdate extends Partial<ProjectCreate> {
  status?: ProjectStatus
  auto_analyze?: boolean
  /** 显式 null = 取消项目默认风格 */
  default_style_id?: number | null
}

export type WorldCategory = 'place' | 'faction' | 'item' | 'rule' | 'history' | 'other'

export interface WorldSetting {
  id: number
  project_id: number
  title: string
  content: string
  /** 后端为 str；正常取值为 WorldCategory */
  category: string
  sort: number
}

export interface WorldSettingInput {
  title: string
  content?: string
  category?: WorldCategory
  sort?: number | null
}

export type LifeStatus = 'alive' | 'dead' | 'missing' | 'other'

export interface Character {
  id: number
  project_id: number
  name: string
  role_type: string
  /** 简介 */
  profile: string
  /** 旧关系备注：界面只读 */
  relations_text: string
  aliases: string[]
  age: string
  gender: string
  appearance: string
  personality: string
  background: string
  current_state: string
  life_status: string
  always_in_context: boolean
  sort: number
}

export interface CharacterInput {
  name: string
  role_type?: string
  profile?: string
  relations_text?: string
  /** 单个 ≤50 字，最多 10 个 */
  aliases?: string[]
  age?: string
  gender?: string
  appearance?: string
  personality?: string
  background?: string
  current_state?: string
  life_status?: LifeStatus
  /** 创建时省略 = 按定位推断（主角默认开启） */
  always_in_context?: boolean | null
  sort?: number | null
}

export interface Outline {
  id: number
  project_id: number
  volume_no: number
  volume_title: string
  chapter_no: number
  title: string
  summary: string
  sort: number
}

export interface OutlineInput {
  volume_no?: number
  volume_title?: string
  chapter_no?: number
  title: string
  summary?: string
  sort?: number | null
}

export type ChapterStatus = 'draft' | 'generating' | 'done'

export interface ChapterBrief {
  id: number
  project_id: number
  outline_id: number | null
  /** 对应的细纲（outline-pro）；未按细纲建章时为 null */
  plan_id: number | null
  /** 本章写作风格覆盖（null = 继承项目默认） */
  style_id: number | null
  chapter_no: number
  title: string
  status: ChapterStatus
  word_count: number
  updated_at: string
}

export interface Chapter extends ChapterBrief {
  content: string
}

export interface ChapterCreate {
  outline_id?: number | null
  chapter_no?: number | null
  title?: string | null
  content?: string
}

export interface ChapterUpdate {
  outline_id?: number | null
  /** 显式 null = 继承项目默认风格；省略 = 不变 */
  style_id?: number | null
  chapter_no?: number | null
  title?: string | null
  content?: string | null
  status?: 'draft' | 'done' | null
}

export interface GenerateChapterRequest {
  ai_config_id?: number | null
  instruction?: string
  target_words?: number
  /** 后端默认 false；编辑器默认开启并显式传 true */
  auto_continue?: boolean
  style?: StyleChoice
}

/** generate SSE 的 continue 事件：每轮自动续写开始前，round 从 1 起，最多 2 */
export interface ContinueEvent {
  round: number
  words: number
  target: number
}

/** 章节生成/润色的 done 载荷；warning 仅在自动续写失败时出现 */
export interface ChapterDone {
  chapter: Chapter
  warning?: string
}

export interface PolishChapterRequest {
  ai_config_id?: number | null
  instruction?: string
  style?: StyleChoice
}

export type WizardKind = 'outline' | 'characters' | 'world' | 'careers'

/** 大纲向导 / 大纲导入的写入模式：replace = 覆盖（先自动存快照）；append = 追加到末尾 */
export type OutlineMode = 'replace' | 'append'

export interface WizardRequest {
  ai_config_id?: number | null
  instruction?: string
  /** 仅 outline：默认 replace */
  outline_mode?: OutlineMode
  volume_count?: number
  chapters_per_volume?: number
  character_count?: number
  /** 仅 careers：1–12，默认 4 */
  career_count?: number
  /** 从中断处继续：同项目同类型、失败 / 已取消 / 已中断且有部分输出的向导任务；生成参数沿用该任务（只有 AI 配置可换） */
  continue_from_task_id?: number
}

export interface WizardItemMap {
  outline: Outline
  characters: Character
  world: WorldSetting
  careers: Career
}

/** import = 拆书导入时为每章建的初始版本 */
export type VersionSource = 'manual' | 'baseline' | 'generate' | 'polish' | 'rewrite' | 'restore' | 'revise' | 'import' | 'agent'

export interface ChapterVersionBrief {
  id: number
  chapter_id: number
  seq: number
  source: VersionSource
  word_count: number
  starred: boolean
  note: string
  generation_log_id: number | null
  created_at: string
  updated_at: string
}

export interface ChapterVersion extends ChapterVersionBrief {
  content: string
}

/** 省略或 null = 不变 */
export interface ChapterVersionUpdate {
  starred?: boolean | null
  note?: string | null
}

export type GenerationKind =
  | 'generate'
  | 'polish'
  | 'rewrite'
  | 'expand'
  | 'condense'
  | 'deslop'
  | 'continue'
  | 'revise'
  | 'book_import.digest'
  | 'book_import.synthesize'
  | 'book_import.style'
  | 'agent.turn'

export type GenerationStatus = 'success' | 'error' | 'cancelled'

export interface GenerationLogBrief {
  id: number
  project_id: number
  chapter_id: number | null
  kind: GenerationKind
  ai_config_id: number | null
  provider: string
  model: string
  params: Record<string, unknown>
  status: GenerationStatus
  error: string
  output_chars: number
  duration_ms: number
  /** 目前恒为 null（provider 流式接口拿不到用量） */
  prompt_tokens: number | null
  completion_tokens: number | null
  created_at: string
}

export interface GenerationMessage {
  role: string
  content: string
}

export interface GenerationLog extends GenerationLogBrief {
  messages: GenerationMessage[]
}

export type RewriteOp = 'rewrite' | 'expand' | 'condense' | 'deslop' | 'continue'

/** 偏移均为码点偏移；op=continue 时 start === end 表示光标位置 */
export interface RewriteRequest {
  ai_config_id?: number | null
  op: RewriteOp
  start: number
  end: number
  instruction?: string
  style?: StyleChoice
}

/** 局部改写 done 载荷：正文未改，需调用 apply 采纳 */
export interface RewriteDone {
  text: string
  start: number
  end: number
  original: string
  /** 选区（光标）前最多 200 码点的原文；采纳时原样传回做乐观校验 */
  before: string
  log_id: number | null
}

export interface RewriteApply {
  start: number
  end: number
  original: string
  /** 预览 done 载荷的 before；光标续写 original 为空，必须靠它发现前文变化 */
  before?: string | null
  replacement: string
  log_id?: number | null
}

// ---------------- 后台任务（backend/app/schemas/task.py） ----------------

export type TaskKind =
  | 'chapter.generate'
  | 'chapter.polish'
  | 'wizard.outline'
  | 'wizard.characters'
  | 'wizard.world'
  | 'character.generate'
  | 'organization.generate'
  | 'relations.parse'
  | 'career.generate'
  | 'wizard.careers'
  | 'chapter.analyze'
  | 'chapter.revise'
  | 'outline.continue'
  | 'outline.expand'
  | 'chapter.batch'
  | 'kb.index'
  | 'book.import'
  | 'agent.turn'

/** paused = 待审核（多步任务到达审核点）：不占并发名额，可继续 / 结束 / 取消，不能删除 / 重试 */
export type TaskStatus = 'queued' | 'running' | 'paused' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'

export interface Task {
  id: number
  kind: TaskKind
  status: TaskStatus
  /** 后端生成的展示标题，如「第 3 章 xx · AI 生成」「大纲向导」 */
  title: string
  project_id: number | null
  chapter_id: number | null
  /** 0–100 */
  progress: number
  stage: string
  error: string
  retry_of_id: number | null
  /** 任务针对的角色（目前仅 relations.parse 有值），用于按角色接上活动任务 */
  character_id: number | null
  /** 按建议重写引用的分析（仅 chapter.revise 有值），用于重写前后评分对比 */
  analysis_id: number | null
  /** 展开的大纲（仅 outline.expand 有值），用于按大纲接上活动任务 */
  outline_id: number | null
  /**
   * 多步任务的断点（首次运行前为 null）：chapter.batch 为 BatchCheckpoint；book.import 为断点摘要 BookImportSummary。
   * 按 kind 收窄：批量用 utils/batchState.ts 的 batchCheckpoint，拆书用 utils/bookImport.ts 的 bookImportSummary（任务面板只用 bookImportState.ts 的轻量读取）
   */
  checkpoint: BatchCheckpoint | BookImportSummary | AgentCheckpoint | null
  /** 断点：下一步要处理的章节在 checkpoint.items 中的下标（仅多步任务有意义） */
  step_cursor: number
  /** 仅 queued 有值：前面还在排队的任务数（0 = 下一个执行） */
  queue_position: number | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface TaskListQuery {
  project_id?: number
  chapter_id?: number
  kind?: TaskKind
  /** true = 未结束（排队 + 运行中 + 待审核）；false = 已结束；省略 = 全部 */
  active?: boolean
  /** 默认 50，最大 200 */
  limit?: number
}

export interface TaskOutput {
  text: string
}

export interface TaskClearResult {
  deleted: number
}

/** 订阅 progress 事件载荷 */
export interface TaskProgress {
  status: TaskStatus
  progress: number
  stage: string
  queue_position?: number | null
}

// ---------------- 批量顺序生成（backend/app/schemas/batch.py、services/batch_service.py） ----------------

export interface BatchGenerateRequest {
  start_chapter_id: number
  end_chapter_id: number
  include_written?: boolean
  /** 300–20000，默认 3000 */
  target_words?: number
  /** 章节有细纲时用细纲 est_words（默认 true）；false = 统一字数覆盖全部章节 */
  use_plan_words?: boolean
  auto_continue?: boolean
  /** 0 = 不暂停；1–50 = 每 N 章进入待审核 */
  review_every?: number
  skip_on_fail?: boolean
  /** ≤2000 */
  instruction?: string
  ai_config_id?: number | null
  /** inherit = 按章节覆盖或项目默认（逐章解析）；none = 不用风格；数字 = 全部章节使用该风格；续跑 / 重试沿用 */
  style?: StyleChoice
}

export type BatchWordsSource = 'plan' | 'uniform'

export interface BatchPreviewItem {
  chapter_id: number
  chapter_no: number
  title: string
  target_words: number
  words_source: BatchWordsSource
  /** 已写章节（勾选包含已写时会被重写） */
  written: boolean
}

export interface BatchSkippedItem {
  chapter_id: number
  chapter_no: number
  title: string
  word_count: number
}

export interface BatchPreview {
  /** 范围内章节总数 */
  total: number
  items: BatchPreviewItem[]
  skipped: BatchSkippedItem[]
  analyze: boolean
  estimated_minutes: number
  warnings: string[]
}

export type BatchItemState = 'pending' | 'done' | 'skipped_written' | 'skipped_missing' | 'failed'
export type BatchAnalysisState = 'ok' | 'skipped' | 'failed'

/** checkpoint.items 的一项（与 input.chapter_ids 一一对应） */
export interface BatchItem {
  chapter_id: number
  chapter_no: number | null
  title: string
  state: BatchItemState
  target_words: number
  words_source?: BatchWordsSource
  words: number
  analysis: BatchAnalysisState | null
  warning: string
  error: string
}

export interface BatchCheckpoint {
  /** cursor 所指章节的阶段：analyze = 正文已写好、还没分析 */
  phase: 'generate' | 'analyze'
  current: { chapter_id: number; prev_status: string } | null
  since_review: number
  items: BatchItem[]
}

export interface BatchChapterRef {
  chapter_id: number
  chapter_no: number | null
  title: string
}

/** done.result / paused.result / 结束后的 result */
export interface BatchSummary {
  total: number
  done: number
  skipped_written: number
  skipped_missing: number
  failed: (BatchChapterRef & { error: string })[]
  analysis_failed: (BatchChapterRef & { warning: string })[]
  remaining: number
  warnings: string[]
  ended_early?: boolean
}

/** 订阅事件 chapter_start */
export interface BatchChapterStart {
  index: number
  total: number
  chapter_id: number
  chapter_no: number | null
  title: string
  target_words: number
}

/** 订阅事件 chapter_end */
export interface BatchChapterEnd {
  index: number
  item: BatchItem
}

// ---------------- 拆书导入（backend/app/schemas/book_import.py、services/book_import/reverse.py） ----------------

export type BookKind = 'txt' | 'md' | 'docx' | 'epub'
export type BookSplitMode = 'auto' | 'regex'
/** short < 300 字、long > 2 万字、dup_title 标题重复、gap 序号断档 */
export type BookChapterWarning = 'short' | 'long' | 'dup_title' | 'gap'
export type BookOp = 'rename' | 'merge_next' | 'split' | 'delete' | 'start_volume' | 'rename_volume' | 'clear_volume'
/** [起, 止]：章节序号从 1 起、两端包含 */
export type BookRange = [number, number]

export interface BookImportBrief {
  id: number
  filename: string
  book_title: string
  chapter_count: number
  total_chars: number
  expires_at: string
  updated_at: string
}

export interface BookChapterView {
  /** 从 1 起 */
  index: number
  title: string
  /** 0 = 不归属任何卷（排在第一个卷之前）；没有任何分卷时全部为 1、卷名为空 */
  volume_no: number
  volume_title: string
  /** 从标题解析出的原始序号（只用于序号断档提示） */
  source_no: number | null
  chars: number
  /** 开头一行（≤60 字） */
  first_line: string
  warnings: BookChapterWarning[]
}

export type BookWarningCounts = Record<BookChapterWarning, number>

export interface BookImport {
  id: number
  filename: string
  kind: BookKind
  size: number
  book_title: string
  split_mode: BookSplitMode
  split_pattern: string | null
  /** 修订号：每次校正 / 重切都要带上，过期 → 409 */
  revision: number
  notices: string[]
  chapters: BookChapterView[]
  warning_counts: BookWarningCounts
  chapter_count: number
  total_chars: number
  /** 章节数超过 max_import_chapters：导入范围必须缩小 */
  too_many: boolean
  max_import_chapters: number
  expires_at: string
  created_at: string
  updated_at: string
}

export interface BookChapterText {
  index: number
  title: string
  /** 正文按行切分：行号 = 下标 + 1（拆分时传这个行号，不能是第 1 行） */
  lines: string[]
}

export interface BookResplitRequest {
  mode: BookSplitMode
  /** regex 模式必填，≤100 字 */
  pattern?: string | null
  revision: number
}

export interface BookOpRequest {
  op: BookOp
  index: number
  /** rename / start_volume / rename_volume 必填（1–200 字） */
  title?: string | null
  /** split 必填：章内行号（从 1 起，不能是第 1 行） */
  line?: number | null
  revision: number
}

export interface BookEstimateRequest {
  import_range: BookRange
  reverse_range: BookRange
  extract_style?: boolean
  deep_analyze?: boolean
}

/** 预估的 AI 输入字数（单位：字符） */
export interface BookEstimate {
  import_chapters: number
  import_chars: number
  reverse_chars: number
  batches: number
  digest_input: number
  synthesize_input: number
  style_input: number
  analyze_input: number
  total_input: number
}

export interface BookStartRequest {
  revision: number
  /** 1–200 字 */
  title: string
  /** ≤100 字 */
  genre?: string
  import_range: BookRange
  reverse_range: BookRange
  extract_style?: boolean
  deep_analyze?: boolean
  ai_config_id?: number | null
}

export interface BookStartResult {
  project_id: number
  task: Task
}

export type BookImportPhase = 'digest' | 'synthesize' | 'apply' | 'style' | 'analyze' | 'done'

export interface BookImportStats {
  summaries: number
  characters: number
  organizations: number
  world: number
  careers: number
  relations: number
  analyses_queued: number
  style_id: number | null
  /** 写入的项目字段名（简介、主题等，后端字段名） */
  project_fields: string[]
}

export type BookImportSkipped =
  | { stage: 'digest'; batch_index: number; chapter_nos: number[] }
  | { stage: 'synthesize'; chapter_nos: number[] }

export interface BookImportPauseReason {
  reason: 'failed'
  phase: 'digest' | 'synthesize'
  batch_index: number | null
  error: string
}

/** book.import 的 TaskOut.checkpoint / done.result / paused.result（同一结构；/finish 之后 result 另有 ended_early） */
export interface BookImportSummary {
  phase: BookImportPhase
  batches_total: number
  batches_done: number
  batches_skipped: number
  stats: BookImportStats
  skipped: BookImportSkipped[]
  warnings: string[]
  pause_reason: BookImportPauseReason | null
  ended_early?: boolean
}

/** 订阅事件 batch_end（book.import 通读每批结束） */
export interface BookBatchEnd {
  /** 从 0 起 */
  index: number
  total: number
  state: 'done'
}

// ---------------- 结构化设定（backend/app/schemas/{organization,relationship,card,entity_ai}.py） ----------------

export interface ChapterCast {
  character_ids: number[]
}

export type MemberStatus = 'active' | 'left' | 'expelled' | 'dead'

export interface Organization {
  id: number
  project_id: number
  name: string
  org_type: string
  purpose: string
  location: string
  /** 0–100，null = 未设定 */
  power_level: number | null
  parent_id: number | null
  description: string
  sort: number
  member_count: number
}

export interface OrganizationInput {
  name: string
  org_type?: string
  purpose?: string
  location?: string
  power_level?: number | null
  parent_id?: number | null
  description?: string
  sort?: number | null
}

/** PATCH：省略 = 不变；parent_id / power_level 显式 null = 清空 */
export type OrganizationUpdate = Partial<OrganizationInput>

export interface Member {
  id: number
  organization_id: number
  organization_name: string
  character_id: number
  character_name: string
  position: string
  status: string
  note: string
}

export interface MemberInput {
  character_id: number
  position?: string
  status?: MemberStatus
  note?: string
}

export interface MemberUpdate {
  position?: string | null
  status?: MemberStatus | null
  note?: string | null
}

export interface OrganizationWithMembers {
  organization: OrganizationInput
  members: MemberInput[]
}

export interface OrganizationWithMembersOut {
  organization: Organization
  members: Member[]
}

export type RelationCategory = 'family' | 'social' | 'hostile' | 'work' | 'other'
export type RelationStatus = 'normal' | 'broken' | 'past' | 'complicated'

export interface RelationshipType {
  id: number
  /** null = 全局预置（只读） */
  project_id: number | null
  category: string
  role_a: string
  role_b: string
  sort: number
  builtin: boolean
}

export interface RelationshipTypeInput {
  category?: RelationCategory
  role_a: string
  role_b: string
}

/** 指定关系类型：type_id，或 role_a + role_b（匹配不上后端新建自定义类型） */
export interface TypeRef {
  type_id?: number | null
  role_a?: string | null
  role_b?: string | null
  category?: RelationCategory
}

/** 语义：「a 是 b 的 role_a，b 是 a 的 role_b」 */
export interface RelationshipCreate extends TypeRef {
  a_id: number
  b_id: number
  intimacy?: number
  status?: RelationStatus
  description?: string
  source?: 'manual' | 'ai_parse'
  chapter_no?: number | null
  note?: string
}

/** 省略 = 不变；a_id 用于调换方向（必须是这对角色之一） */
export interface RelationshipUpdate {
  a_id?: number | null
  type_id?: number | null
  intimacy?: number | null
  status?: RelationStatus | null
  description?: string | null
  chapter_no?: number | null
  note?: string
}

export interface Relationship {
  id: number
  project_id: number
  a_id: number
  b_id: number
  a_name: string
  b_name: string
  type_id: number
  category: string
  role_a: string
  role_b: string
  intimacy: number
  status: string
  description: string
  created_at: string
  updated_at: string
  /** 以下仅在 ?character_id= 查询时有值 */
  other_id: number | null
  other_name: string | null
  /** 对方在我眼中的身份，如「徒弟」 */
  other_role: string | null
  /** 我自己的身份，如「师父」 */
  self_role: string | null
}

export interface RelationshipHistory {
  id: number
  relationship_id: number
  chapter_no: number | null
  source: string
  /** {字段: [旧, 新]}；type / direction 存名称 */
  changes: Record<string, unknown>
  note: string
  created_at: string
}

/** 随新角色一起保存的关系（subject = 新角色；subject_is_a 为真时新角色是 role_a 一方） */
export interface RelationDraft extends TypeRef {
  target_id: number
  subject_is_a: boolean
  intimacy?: number
  status?: RelationStatus
  description?: string
}

export interface CharacterWithRelations {
  character: CharacterInput
  relations: RelationDraft[]
  /** 勾选的职业挂接（最多 4 个：主 1 副 3），后端以 source=ai 写入 */
  careers?: CareerAssignDraft[]
}

export interface CharacterWithRelationsOut {
  character: Character
  relationships: Relationship[]
  careers: Assignment[]
}

export interface GraphNode {
  id: number
  name: string
  aliases: string[]
  role_type: string
  is_protagonist: boolean
  life_status: string
  organization_ids: number[]
}

export interface GraphEdge {
  id: number
  source: number
  target: number
  type_id: number
  category: string
  role_a: string
  role_b: string
  label: string
  intimacy: number
  status: string
  description: string
}

export interface GraphOrganization {
  id: number
  name: string
  parent_id: number | null
  org_type: string
}

export interface RelationshipGraph {
  nodes: GraphNode[]
  edges: GraphEdge[]
  organizations: GraphOrganization[]
}

// 卡片（ainovel.cards v1）

export interface CardExportRequest {
  character_ids: number[]
  organization_ids: number[]
}

export type CardResolution = 'skip' | 'rename'

export interface CardResolutions {
  characters: Record<string, CardResolution>
  organizations: Record<string, CardResolution>
}

export interface CardPreviewItem {
  name: string
  status: 'new' | 'conflict'
  rename_to: string | null
}

export interface CardPreviewRelationship {
  a_name: string
  b_name: string
  role_a: string
  role_b: string
  status: 'ok' | 'unresolved'
}

/** v2：职业按名匹配目标项目；reuse 时 stages_added 为将追加到阶梯末尾的阶段名 */
export interface CardPreviewCareer {
  name: string
  status: 'new' | 'reuse'
  stages_added: string[]
}

export interface CardPreview {
  characters: CardPreviewItem[]
  organizations: CardPreviewItem[]
  relationships: CardPreviewRelationship[]
  /** v1 卡片为空数组 */
  careers: CardPreviewCareer[]
  warnings: string[]
}

export interface CardImportedItem {
  name: string
  imported_name: string | null
  id: number | null
  action: 'created' | 'renamed' | 'skipped'
}

export interface CardImportedCareer {
  name: string
  id: number
  action: 'created' | 'reused'
  /** 追加到阶梯末尾的阶段数 */
  stages_added: number
}

export interface CardImportResult {
  characters: CardImportedItem[]
  organizations: CardImportedItem[]
  careers: CardImportedCareer[]
  relationships_created: number
  members_created: number
  careers_assigned: number
  warnings: string[]
}

// AI 单体生成 / 旧关系解析（任务 done.result，需经 api/entityTasks.ts 的 parse* 收窄）

export interface CharacterGenerateRequest {
  ai_config_id?: number | null
  instruction?: string
  role_type?: string
}

export interface OrganizationGenerateRequest {
  ai_config_id?: number | null
  instruction?: string
}

export interface RelationsParseRequest {
  ai_config_id?: number | null
}

/** 主体视角的关系建议；resolved=false 不可保存，reason 说明原因 */
export interface RelationSuggestion {
  target_name: string
  target_id: number | null
  resolved: boolean
  reason: string
  existing_relationship_id: number | null
  self_role: string
  target_role: string
  subject_is_a: boolean
  type: { type_id: number | null; role_a: string; role_b: string; category: string }
  intimacy: number
  status: string
  description: string
}

export interface CharacterDraft {
  name: string
  aliases: string[]
  role_type: string
  age: string
  gender: string
  appearance: string
  personality: string
  background: string
  profile: string
  current_state: string
  life_status: string
  always_in_context: boolean
}

/** 职业挂接建议：resolved=false（职业对不上 / 超出主 1 副 3 / 没有阶段）不可勾选，reason 说明原因 */
export interface CareerSuggestion {
  career_name: string
  career_id: number | null
  career_kind: CareerKind | null
  role: CareerKind | null
  stage_name: string
  stage_id: number | null
  stage_position: number | null
  note: string
  resolved: boolean
  reason: string
}

export interface CharacterGenerateResult {
  draft: CharacterDraft
  relation_suggestions: RelationSuggestion[]
  /** 项目没有职业时为空数组 */
  career_suggestions: CareerSuggestion[]
  warnings: string[]
}

export interface OrganizationDraft {
  name: string
  org_type: string
  purpose: string
  location: string
  power_level: number | null
  description: string
  parent_name: string | null
  parent_id: number | null
}

export interface MemberSuggestion {
  character_name: string
  character_id: number | null
  position: string
  resolved: boolean
  reason: string
}

export interface OrganizationGenerateResult {
  draft: OrganizationDraft
  member_suggestions: MemberSuggestion[]
  warnings: string[]
}

export interface RelationsParseResult {
  character_id: number
  character_name: string
  suggestions: RelationSuggestion[]
  warnings: string[]
}

// ---------------- 职业等级体系（backend/app/schemas/career.py） ----------------

/** main = 主修（只能挂成主职业），sub = 副修（只能挂成副职业） */
export type CareerKind = 'main' | 'sub'

export interface CareerStage {
  id: number
  /** 从 1 连续 */
  position: number
  name: string
  description: string
}

/** 整体提交阶段：带 id = 更新，无 id = 新建，缺失 = 删除；数组顺序即新的 position */
export interface StageInput {
  id?: number | null
  name: string
  description?: string
}

export interface Career {
  id: number
  project_id: number
  name: string
  /** 后端为 str；正常取值为 CareerKind */
  kind: string
  description: string
  requirements: string
  abilities: string
  rules: string
  sort: number
  stages: CareerStage[]
  holder_count: number
  created_at: string
  updated_at: string
}

export interface CareerInput {
  name: string
  kind?: CareerKind
  description?: string
  requirements?: string
  abilities?: string
  rules?: string
  /** 至少 1 阶 */
  stages: StageInput[]
  sort?: number | null
}

/** PATCH：省略 = 不变；stages 给出时整体同步 */
export type CareerUpdate = Partial<CareerInput>

export interface CareerHolder {
  id: number
  character_id: number
  character_name: string
  role: string
  stage_id: number
  stage_name: string
  stage_position: number
  stage_total: number
  note: string
}

/** 角色的职业挂接（主职业在前） */
export interface Assignment {
  id: number
  character_id: number
  career_id: number
  career_name: string
  career_kind: string
  role: string
  stage_id: number
  stage_name: string
  stage_position: number
  stage_total: number
  note: string
  updated_at: string
}

export interface AssignmentInput {
  career_id: number
  /** 省略 = 第 1 阶 */
  stage_id?: number | null
  note?: string
  /** 给出时必须等于职业类别 */
  role?: CareerKind | null
  chapter_no?: number | null
  history_note?: string
}

/** 省略 = 不变；chapter_no / history_note 只记入历史 */
export interface AssignmentUpdate {
  stage_id?: number | null
  note?: string | null
  chapter_no?: number | null
  history_note?: string
}

export interface CareerHistory {
  id: number
  character_id: number
  career_id: number | null
  career_name: string
  source: string
  chapter_no: number | null
  /** {role|stage|note|removed: [旧, 新]}；阶段以「第N阶·名称」记录 */
  changes: Record<string, unknown>
  note: string
  created_at: string
}

export interface CareerAssignDraft {
  career_id: number
  stage_id?: number | null
  note?: string
}

export interface CareerGenerateRequest {
  ai_config_id?: number | null
  /** 1–5，默认 3 */
  count?: number
  /** 省略 = 由模型决定 */
  kind?: CareerKind | null
  instruction?: string
}

export interface CareerDraftStage {
  name: string
  description: string
}

export interface CareerDraft {
  name: string
  kind: CareerKind
  description: string
  requirements: string
  abilities: string
  rules: string
  stages: CareerDraftStage[]
  /** 与已有职业同名：需改名后才能保存 */
  conflict: boolean
}

export interface CareerGenerateResult {
  drafts: CareerDraft[]
  warnings: string[]
}

// ---------------- 章节分析与回写（backend/app/schemas/analysis.py、chapter.py ReviseChapterRequest） ----------------

/** none = 未分析；analyzing = 有排队 / 运行中的分析任务；analyzed = 最新分析与正文一致；stale = 正文已变化 */
export type AnalysisStatus = 'none' | 'analyzing' | 'analyzed' | 'stale'

export type ChangeKind =
  | 'character_state'
  | 'life_status'
  | 'relationship_update'
  | 'relationship_create'
  | 'career_stage'
  | 'org_membership'
  | 'foreshadow_resolve'
  | 'foreshadow_plant_planned'
  | 'foreshadow_new'

export type ChangeStatus = 'applied' | 'pending' | 'reverted' | 'rejected' | 'skipped' | 'failed'

export type ScoreKey = 'overall' | 'pacing' | 'engagement' | 'coherence'

/** 1.0–10.0，一位小数；模型没给出的项为 null；overall = 有值项的平均（服务端复算） */
export type AnalysisScores = Partial<Record<ScoreKey, number | null>>

/** 本地规则计算；dialogue_ratio 为 0–1 */
export interface AnalysisMetrics {
  words: number
  paragraphs: number
  avg_paragraph_chars: number
  dialogue_ratio: number
}

export interface AnalyzeRequest {
  ai_config_id?: number | null
}

export interface AnalyzePendingResult {
  queued: number
  task_ids: number[]
}

export interface ChapterAnalysisState {
  chapter_id: number
  status: AnalysisStatus
  latest_analysis_id: number | null
  scores: AnalysisScores | null
  analyzed_at: string | null
  /** 排队 / 运行中的分析任务（最新一个） */
  active_task_id: number | null
}

export interface AnalysisBrief {
  id: number
  chapter_id: number
  chapter_no: number
  task_id: number | null
  model: string
  scores: AnalysisScores
  /** 后端为 dict；正常结构为 AnalysisMetrics */
  metrics: Partial<AnalysisMetrics>
  /** 正文已变化（分析时的正文 hash ≠ 当前正文） */
  stale: boolean
  created_at: string
}

export interface PlotPoint {
  text: string
  /** 1–5 */
  importance: number
}

export interface Hook {
  /** 悬念 | 冲突 | 情感 | 反转 | 其他 */
  type: string
  /** 1–10 */
  strength: number
  text: string
}

export interface AnalysisConflict {
  level: number | null
  types: string[]
}

export interface AnalysisEmotion {
  tone: string
  start: string
  middle: string
  end: string
}

export type SuggestionPriority = 'high' | 'medium' | 'low'

export interface Suggestion {
  /** 本次分析内从 1 起 */
  id: number
  /** 节奏 | 对话 | 描写 | 冲突 | 人物 | 其他 */
  category: string
  priority: SuggestionPriority
  text: string
}

/**
 * 回写条目。target / before / after 的结构随 kind 变化（见 api/analysis.ts 的 describeChange）；
 * 未匹配到对象的条目 target 只含原始名字、field_key 为空串、status = skipped、原因在 reason。
 */
export interface AnalysisChange {
  id: number
  analysis_id: number
  chapter_no: number
  kind: ChangeKind
  target: Record<string, unknown>
  field_key: string
  before: unknown
  after: unknown
  status: ChangeStatus
  reason: string
  applied_at: string | null
  resolved_at: string | null
  created_at: string
}

export interface Analysis extends AnalysisBrief {
  summary: string
  plot_points: PlotPoint[]
  hooks: Hook[]
  conflict: Partial<AnalysisConflict>
  emotion: Partial<AnalysisEmotion>
  changes_raw: Record<string, unknown>
  /** 伏笔候选（规范化后为 ForeshadowCandidate；经 api/foreshadows.ts 的 readCandidates 收窄） */
  foreshadow_candidates: unknown[]
  suggestions: Suggestion[]
  warnings: string[]
  changes: AnalysisChange[]
}

/** chapter.analyze 的 done.result（经 parseAnalyzeDone 收窄） */
export interface AnalyzeDone {
  analysis_id: number
  chapter_id: number
  chapter_no: number
  summary: string
  scores: AnalysisScores
  change_counts: Partial<Record<ChangeStatus, number>>
  warnings: string[]
}

/** 按建议整章重写（chapter.revise）：至少勾选一条建议或填写 instruction */
export interface ReviseChapterRequest {
  analysis_id: number
  /** 最多 20 条 */
  suggestion_ids: number[]
  /** ≤2000 字 */
  instruction: string
  /** 最多 10 项 */
  preserve: string[]
  /** 300–20000；省略 = 原文字数 */
  target_words?: number | null
  ai_config_id?: number | null
  style?: StyleChoice
}

// ---------------- 伏笔（backend/app/schemas/foreshadow.py） ----------------

/** pending 待埋 / planted 已埋 / resolved 已回收 / partial 部分回收 / abandoned 已废弃 */
export type ForeshadowStatus = 'pending' | 'planted' | 'resolved' | 'partial' | 'abandoned'

export type ForeshadowSort = 'planned_resolve' | 'importance'

export interface Foreshadow {
  id: number
  project_id: number
  title: string
  content: string
  /** 埋法要点 */
  plant_hint: string
  /** 回收要点 */
  reveal_hint: string
  status: ForeshadowStatus
  /** 四个章节字段存章节号（可指向尚未创建的章节） */
  planned_plant_chapter: number | null
  actual_plant_chapter: number | null
  planned_resolve_chapter: number | null
  actual_resolve_chapter: number | null
  /** 1–5 */
  importance: number
  include_in_context: boolean
  note: string
  /** manual | analysis */
  source: string
  /** 与 character_names 一一对应 */
  character_ids: number[]
  character_names: string[]
  /** 派生：已埋 / 部分回收且没有计划回收章 */
  is_long_term: boolean
  /** 派生：超期章数，0 = 未超期（列表以项目当前章为参照，提醒以该章为参照） */
  overdue_by: number
  created_at: string
  updated_at: string
}

export interface ForeshadowInput {
  title: string
  content?: string
  plant_hint?: string
  reveal_hint?: string
  status?: ForeshadowStatus
  planned_plant_chapter?: number | null
  actual_plant_chapter?: number | null
  planned_resolve_chapter?: number | null
  actual_resolve_chapter?: number | null
  /** 1–5，默认 3 */
  importance?: number
  include_in_context?: boolean
  note?: string
  /** ≤50，须属于本项目 */
  character_ids?: number[]
}

/** 省略 = 不变；章节字段显式 null = 清空；character_ids 给出时整体替换；chapter_no / history_note 只记入历史 */
export interface ForeshadowUpdate extends Partial<ForeshadowInput> {
  chapter_no?: number | null
  /** ≤200 */
  history_note?: string
}

export interface ForeshadowListQuery {
  status?: ForeshadowStatus
  overdue?: boolean
  long_term?: boolean
  character_id?: number
  sort?: ForeshadowSort
}

export interface ForeshadowHistory {
  id: number
  foreshadow_id: number
  source: string
  chapter_no: number | null
  /** {字段: [旧, 新]}；关联角色以名字列表记在 characters 键 */
  changes: Record<string, unknown>
  note: string
  created_at: string
}

export interface ForeshadowStats {
  pending: number
  planted: number
  resolved: number
  partial: number
  abandoned: number
  overdue: number
  long_term: number
  /** 已有正文的最大章节号（没有时为 0），超期按它判断 */
  current_chapter: number
}

/** 某一章的伏笔提醒（与章节上下文【伏笔提醒】同源，只含注入上下文开启的伏笔） */
export interface ForeshadowReminders {
  chapter_no: number
  to_plant: Foreshadow[]
  must_resolve: Foreshadow[]
  overdue: Foreshadow[]
  upcoming: Foreshadow[]
  long_term: Foreshadow[]
}

/** 确认分析识别的新伏笔时可修改的字段（省略 = 用分析给出的值；planned_resolve_chapter 显式 null = 清空） */
export interface ForeshadowOverrides {
  title?: string
  content?: string
  plant_hint?: string
  reveal_hint?: string
  planned_resolve_chapter?: number | null
  importance?: number
  character_ids?: number[]
}

/** 分析详情 foreshadow_candidates 的规范化结构 */
export interface ForeshadowCandidate {
  action: 'plant' | 'resolve'
  ref_id: number | null
  text: string
  keyword: string
  expected_resolve_chapter: number | null
  partial: boolean
}

// ---------------- 大纲进阶（backend/app/schemas/outline.py，outline-pro） ----------------

/** 细纲（chapter_plans） */
export interface Plan {
  id: number
  project_id: number
  outline_id: number
  /** 大纲内顺序，从 1 起 */
  seq: number
  title: string
  summary: string
  key_events: string[]
  /** 已过滤掉被删除的角色 */
  character_ids: number[]
  emotion: string
  goal: string
  /** 300–20000 */
  est_words: number
  /** 已建章时有值 */
  chapter_id: number | null
  chapter_no: number | null
  /** 已建章且章节有正文：不能删除 */
  has_content: boolean
  created_at: string
  updated_at: string
}

export interface PlanFields {
  title: string
  summary?: string
  key_events?: string[]
  character_ids?: number[]
  emotion?: string
  goal?: string
  est_words?: number
}

/** 整体同步的一条：带 id = 更新已有细纲，无 id = 新建 */
export interface PlanInput extends PlanFields {
  id?: number | null
}

/** PUT /outlines/{oid}/plans：缺失的细纲被删除，顺序即 seq */
export interface PlansSync {
  plans: PlanInput[]
}

/** PATCH /outlines/{oid}/plans/{id}：省略 = 不变 */
export type PlanUpdate = Partial<PlanFields>

export interface PlanBuildResult {
  chapters: ChapterBrief[]
  plans: Plan[]
}

/** GET /outlines/tree 的一行 */
export interface OutlineDetail extends Outline {
  /** 「1 纲 1 章」关联的章节（未展开时）；已展开时为 null，章节见 plans[].chapter_id */
  chapter_id: number | null
  plans: Plan[]
  plan_count: number
  built_count: number
}

export interface OutlineAppendItem {
  volume_no: number
  volume_title: string
  title: string
  summary: string
}

export type OutlineMoveDirection = 'up' | 'down'

export type SnapshotReason = 'wizard_replace' | 'import_replace' | 'reexpand' | 'manual' | 'restore'

export interface OutlineSnapshot {
  id: number
  reason: SnapshotReason
  note: string
  created_at: string
  outline_count: number
  plan_count: number
  link_count: number
}

export interface SnapshotOutlineBrief {
  volume_no: number
  volume_title: string
  chapter_no: number
  title: string
  plan_count: number
}

export interface OutlineSnapshotDetail extends OutlineSnapshot {
  outlines: SnapshotOutlineBrief[]
  /** 恢复前的提示（如快照保存后章节号发生过平移） */
  warnings: string[]
}

export interface SnapshotRestoreResult {
  /** 恢复前为当前状态自动保存的快照：再恢复它即可撤销本次恢复 */
  backup: OutlineSnapshot
  outlines: number
  plans: number
  links_restored: number
  warnings: string[]
}

export interface OutlineImportPreview {
  volumes: number
  outlines: number
  plans: number
  unresolved_characters: string[]
  warnings: string[]
}

export interface OutlineImportResult extends OutlineImportPreview {
  mode: OutlineMode
  /** replace 时替换前自动保存的快照 */
  snapshot_id: number | null
  items: Outline[]
}

export interface OutlineContinueRequest {
  ai_config_id?: number | null
  /** 1–20，默认 5 */
  count?: number
  direction?: string
  /** 给出 = 开新卷；省略 / null = 接在当前最后一卷之后 */
  new_volume?: { title: string } | null
}

export interface OutlineExpandRequest {
  ai_config_id?: number | null
  /** 展开后的总章数（含保留的有正文细纲），2–10 */
  count?: number
  instruction?: string
}

export interface OutlineContinueDraft {
  volume_no: number
  volume_title: string
  chapter_no: number
  title: string
  summary: string
}

/** outline.continue 的 done.result */
export interface OutlineContinueResult {
  drafts: OutlineContinueDraft[]
  warnings: string[]
}

export interface ExpandDraftCharacter {
  name: string
  character_id: number | null
  resolved: boolean
}

export interface OutlineExpandDraft {
  title: string
  summary: string
  key_events: string[]
  characters: ExpandDraftCharacter[]
  emotion: string
  goal: string
  est_words: number
}

export interface OutlineExpandExisting {
  id: number
  seq: number
  title: string
  summary: string
  chapter_id: number | null
  chapter_no: number | null
  has_content: boolean
}

/** outline.expand 的 done.result */
export interface OutlineExpandResult {
  outline_id: number
  drafts: OutlineExpandDraft[]
  existing: OutlineExpandExisting[]
  warnings: string[]
}

// ---------------- 写作风格库（backend/app/schemas/style.py） ----------------

/** 生成时的风格选择：inherit = 章节覆盖 > 项目默认；none = 不用风格；数字 = 指定风格 id */
export type StyleChoice = 'inherit' | 'none' | number

/** 风格字段（全部选填）；长度与后端一致 */
export interface StyleFields {
  /** ≤200 */
  description: string
  /** 以下维度 ≤500 */
  pov: string
  rhythm: string
  dialogue: string
  description_density: string
  tone: string
  diction: string
  /** ≤1000 */
  taboos: string
  /** ≤2000 */
  sample: string
  /** ≤5000 */
  extra: string
}

export interface StyleReferences {
  projects: number
  chapters: number
}

export interface WritingStyle extends StyleFields {
  id: number
  name: string
  preset_key: string | null
  /** 预设：所有用户可见、只读，可复制 */
  is_preset: boolean
  sort: number
  /** 只统计本人的项目 / 章节 */
  references: StyleReferences
  created_at: string
  updated_at: string
}

export interface StyleBrief {
  id: number
  name: string
  description: string
  is_preset: boolean
}

export interface StyleCreate extends Partial<StyleFields> {
  /** 1–100 */
  name: string
}

/** 省略 = 不变 */
export interface StyleUpdate extends Partial<StyleFields> {
  name?: string
  sort?: number
}

export interface StyleExtractRequest {
  /** 200–10000 字 */
  sample_text: string
  ai_config_id?: number | null
}

/** 未保存的风格字段（提取结果 / 试写）；名称可空 */
export interface StyleDraft extends StyleFields {
  name: string
}

/** 提取的 done 载荷：草稿不落库 */
export interface StyleExtractDone {
  draft: StyleDraft
  warnings: string[]
}

export interface StyleTrialRequest {
  /** 二选一：已保存的风格 id，或未保存的风格字段 */
  style_id?: number | null
  style?: StyleDraft | null
  /** 1–500 */
  scene: string
  /** 200–1000，默认 500 */
  words?: number
  ai_config_id?: number | null
}

export interface StyleTrialDone {
  text: string
}

export interface StyleImportItem {
  id: number
  name: string
  /** 同名改名时的原名 */
  renamed_from: string | null
}

export interface StyleImportResult {
  items: StyleImportItem[]
  warnings: string[]
}

// ---------------- 提示词模板（backend/app/schemas/prompt.py） ----------------

export type TemplatePart = 'system' | 'user'

/** 模板校验问题：part / line 为空表示整体问题（如渲染失败） */
export interface TemplateIssue {
  part: TemplatePart | null
  line: number | null
  message: string
}

/** 变量类型：后端为 str，正常取值 text | int | bool | list */
export interface TemplateVar {
  name: string
  label: string
  kind: string
  required: boolean
  /** 锁定变量：内容由代码提供，必须以 {{ name }} 出现且只出现一次，不能放进 if / for */
  locked: boolean
  sample: unknown
}

export interface LockedBlock {
  name: string
  label: string
  value: string
}

export interface PromptTemplateBrief {
  key: string
  name: string
  category: string
  category_label: string
  description: string
  overridden: boolean
  enabled: boolean
  /** 覆盖保存后系统默认已变化 */
  outdated: boolean
  updated_at: string | null
}

export interface PromptOverride {
  system: string
  user: string
  enabled: boolean
  base_fingerprint: string
  /** 保存覆盖时的系统默认全文（三方对比的「旧默认」） */
  base_system: string
  base_user: string
  updated_at: string
}

export interface PromptTemplateDetail extends PromptTemplateBrief {
  default_system: string
  default_user: string
  default_fingerprint: string
  variables: TemplateVar[]
  locked: LockedBlock[]
  override: PromptOverride | null
}

export interface PromptSave {
  system?: string
  user: string
  /** ≤200 */
  note?: string
}

export interface PromptPatch {
  enabled?: boolean | null
  /** 确认已阅新默认：内容不变，base 更新为当前系统默认 */
  ack_default?: boolean
}

export interface PromptVersion {
  id: number
  system: string
  user: string
  note: string
  created_at: string
}

export interface PromptPreviewRequest {
  system?: string
  user: string
  /** 变量值；缺省用示例值（锁定变量始终用代码值） */
  variables?: Record<string, unknown>
}

export interface PromptPreview {
  messages: GenerationMessage[]
  errors: TemplateIssue[]
  locked: LockedBlock[]
}

export type PromptImportStatus = 'created' | 'updated' | 'unchanged' | 'default' | 'skipped' | 'invalid'

export interface PromptImportItemResult {
  key: string
  status: PromptImportStatus
  message: string
  errors: TemplateIssue[]
}

export interface PromptImportResult {
  items: PromptImportItemResult[]
}

// ---------------- 灵感模式（schemas/inspiration.py） ----------------

/** AI 生成选项的固定步骤（顺序即 stale 传播顺序） */
export type InspirationStepKey = 'title' | 'synopsis' | 'theme' | 'tags' | 'selling_points' | 'audience'
/** 会话当前所在步骤：想法 idea 是第 0 步，规模 / 确认不调用 AI */
export type InspirationCurrent = 'idea' | InspirationStepKey | 'scale' | 'confirm'
export type InspirationStepStatus = 'pending' | 'decided' | 'skipped'
/** project = 从项目预填（只由后端写入） */
export type InspirationStepSource = 'ai' | 'edited' | 'manual' | 'project'
export type InspirationSessionStatus = 'active' | 'converted'

export interface InspirationRound {
  options: string[]
  feedback: string
  /** ISO 时间（带时区） */
  created_at: string
}

export interface InspirationStepState {
  /** tags 步骤为 string[]，其余为 string；未定 / 跳过为 null */
  value: string | string[] | null
  status: InspirationStepStatus
  /** 前面的步骤被修改后为 true（保留原值，提示「可能需要更新」） */
  stale: boolean
  source: InspirationStepSource | null
  /** 最多保留最近 10 轮 */
  rounds: InspirationRound[]
}

export interface InspirationScale {
  target_words: number | null
  volume_count: number
  chapters_per_volume: number
  character_count: number
}

export interface InspirationState {
  idea: string
  notes: string[]
  /** 从已有项目发起时项目的题材 */
  genre: string
  steps: Record<InspirationStepKey, InspirationStepState>
  scale: InspirationScale | null
  current: InspirationCurrent
}

export interface InspirationSession {
  id: number
  title: string
  status: InspirationSessionStatus
  project_id: number | null
  converted_project_id: number | null
  state: InspirationState
  created_at: string
  updated_at: string
}

export interface InspirationSessionBrief {
  id: number
  title: string
  status: InspirationSessionStatus
  project_id: number | null
  converted_project_id: number | null
  /** 已定或已跳过的步骤数 / 步骤总数（6） */
  done_steps: number
  total_steps: number
  stale_steps: number
  current: InspirationCurrent
  created_at: string
  updated_at: string
}

/** PATCH /sessions/{id}：给出的字段才修改；修改 idea 时所有已定步骤标为 stale；scale 显式 null = 清空 */
export interface InspirationSessionUpdate {
  idea?: string
  current?: InspirationCurrent
  scale?: InspirationScale | null
}

/** decide：定下 value；skip：跳过；keep：保持不变（清除 stale）；clear：回到待定 */
export interface InspirationStepUpdate {
  action: 'decide' | 'skip' | 'keep' | 'clear'
  value?: string | string[] | null
  source?: 'ai' | 'edited' | 'manual'
}

export interface InspirationGenerateRequest {
  /** ≤500 */
  feedback?: string
  ai_config_id?: number | null
}

/** 生成选项 done 载荷 */
export interface InspirationGenerateDone {
  round: InspirationRound
  session: InspirationSession
}

/** 生成选项 retry 事件：输出不合格，清空已显示原文后重试 */
export interface InspirationRetryEvent {
  attempt: number
  max: number
  reason: string
}

export interface InspirationConvertRequest {
  title_override?: string | null
  scale?: InspirationScale | null
}

export type InspirationApplyField = InspirationStepKey | 'premise' | 'target_words'

export interface InspirationApplyPreviewItem {
  field: InspirationApplyField
  label: string
  current: string | string[] | number | null
  proposed: string | string[] | number | null
  /** 项目中该字段为空时默认勾选 */
  default_checked: boolean
}

export interface InspirationApplyRequest {
  project_id: number
  fields: InspirationApplyField[]
}

// ---------------- 知识库（backend/app/schemas/kb.py） ----------------

/** parsing 目前不使用（解析在上传请求内同步完成）；ready_keyword_only = 已分块但缺少当前向量模型的向量 */
export type KbDocumentStatus = 'pending' | 'parsing' | 'indexing' | 'ready' | 'ready_keyword_only' | 'failed'
/** hybrid = 向量 + 关键词混合；keyword = 只用关键词 */
export type SearchMode = 'hybrid' | 'keyword'

export interface KbSettings {
  auto_inject: boolean
  /** 1–20 */
  top_k: number
  /** 0–1 */
  min_score: number
  /** 500–8000 */
  max_chars: number
  /** 1–20 */
  memory_top_k: number
  /** 300–6000 */
  memory_max_chars: number
  /** 0–1 */
  memory_min_score: number
}

/** 省略 = 不变 */
export type KbSettingsUpdate = Partial<KbSettings>

export interface KbDocument {
  id: number
  project_id: number
  filename: string
  mime: string
  size: number
  sha256: string
  status: KbDocumentStatus
  error: string
  warnings: string[]
  char_count: number
  chunk_count: number
  inject: boolean
  created_at: string
  updated_at: string
}

export interface KbUploadResult {
  document: KbDocument
  task: Task
}

export interface KbChunk {
  id: number
  seq: number
  heading_path: string
  content: string
  start_offset: number
  end_offset: number
}

export interface KbChunkPage {
  items: KbChunk[]
  total: number
  page: number
  page_size: number
}

export interface KbSearchRequest {
  query: string
  /** 1–50，省略 = 项目设置 */
  top_k?: number | null
  /** 0–1，省略 = 项目设置 */
  min_score?: number | null
}

export interface KbSearchHit {
  chunk_id: number
  document_id: number
  filename: string
  heading_path: string
  content: string
  score: number
  /** 只用关键词时为 null */
  vector_score: number | null
  keyword_score: number
}

export interface KbSearchResult {
  hits: KbSearchHit[]
  mode: SearchMode
  warnings: string[]
  candidates: number
}

export interface IndexStatus {
  /** 当前默认向量模型（配置名 · 模型名）；未配置时为 null（只用关键词检索） */
  embedding_model: string | null
  /** 存在缺少当前向量模型向量的检索单元 */
  needs_rebuild: boolean
  stale_count: number
  stale: { kb: number; memory: number }
  /** 项目有排队 / 运行中的建索引任务 */
  indexing: boolean
}

// ---------------- 剧情记忆（backend/app/schemas/memory.py） ----------------

export type MemoryKind = 'summary' | 'plot_point' | 'hook' | 'character' | 'foreshadow' | 'manual'
export type MemoryOrigin = 'auto' | 'manual'

export interface Memory {
  id: number
  project_id: number
  chapter_id: number | null
  chapter_no: number
  kind: MemoryKind
  content: string
  characters: string[]
  /** 1–5 */
  importance: number
  origin: MemoryOrigin
  analysis_id: number | null
  pinned: boolean
  muted: boolean
  created_at: string
  updated_at: string
}

export interface MemoryPage {
  items: Memory[]
  total: number
  page: number
  page_size: number
}

export interface MemoryListQuery {
  kind?: MemoryKind
  origin?: MemoryOrigin
  chapter_from?: number
  chapter_to?: number
  pinned?: boolean
  muted?: boolean
  /** ≤200 */
  q?: string
  page?: number
  /** 1–200，默认 50 */
  page_size?: number
}

export interface MemoryCreate {
  chapter_no: number
  /** ≤1000 */
  content: string
  /** ≤20 个 */
  characters?: string[]
  /** 1–5，默认 3 */
  importance?: number
  pinned?: boolean
}

/** 省略 = 不变；chapter_no / content / characters / importance 只有手动记忆可以修改 */
export interface MemoryUpdate {
  pinned?: boolean
  muted?: boolean
  chapter_no?: number
  content?: string
  characters?: string[]
  importance?: number
}

export interface MemorySearchRequest {
  query: string
  /** 按第 N 章召回：只在第 N 章之前的记忆中检索 */
  before_chapter_no?: number | null
  top_k?: number | null
  min_score?: number | null
  include_muted?: boolean
}

export interface MemorySearchHit {
  memory: Memory
  score: number
  vector_score: number | null
  keyword_score: number
}

export interface MemorySearchResult {
  hits: MemorySearchHit[]
  mode: SearchMode
  warnings: string[]
  candidates: number
}

/** GET /chapters/{cid}/context-preview：本次生成会注入的两块（带标题行、裁剪前；空串 = 不注入） */
export interface ContextPreview {
  knowledge: string
  memories: string
}

// ---- 项目备份与导入（ainovel.project，见 backend/app/schemas/transfer.py） ----

/** 备份可选项（核心数据始终包含）；查询参数里 generation_logs 写作 logs */
export interface BackupOptions {
  versions: boolean
  snapshots: boolean
  generation_logs: boolean
  inspiration: boolean
}

export type BackupOptionKey = keyof BackupOptions

export interface EstimateGroup {
  rows: number
  chars: number
  /** 粗估的文件字节数（chars × 3 + rows × 150） */
  bytes: number
}

/** GET /projects/{pid}/backup/estimate */
export interface BackupEstimate {
  core: EstimateGroup
  versions: EstimateGroup
  snapshots: EstimateGroup
  generation_logs: EstimateGroup
  inspiration: EstimateGroup
}

/** preset = 对应到预设；reuse = 复用同名同内容；create = 新建（name 为导入后的名称）；copy = 预设缺失，复制为自定义风格 */
export type ImportStyleAction = 'preset' | 'reuse' | 'create' | 'copy'

export interface ImportStyleBrief {
  name: string
  preset_key: string | null
  action: ImportStyleAction
}

/** POST /projects/import/preview（multipart file）；文件不合法 → 400，ApiError.body.errors 为前 20 条问题 */
export interface ImportPreview {
  token: string
  title: string
  /** 实际导入时使用的书名（重名时加「（导入）」） */
  final_title: string
  version: number
  exported_at: string | null
  app_schema: string | null
  options: BackupOptions
  /** 表名 → 行数（只含非空的表） */
  counts: Record<string, number>
  styles: ImportStyleBrief[]
  warnings: string[]
  /** 令牌有效期（秒） */
  expires_in: number
}

export interface ImportRequest {
  token: string
  title?: string | null
}

/** POST /projects/import → 201 */
export interface ImportResult {
  project: Project
  counts: Record<string, number>
  warnings: string[]
  /** 有向量模型时排队的重建索引任务 */
  task_id: number | null
}

// ---------------- 项目智能体（backend/app/schemas/agent.py） ----------------

export interface AgentPageContext {
  route: string
  chapter_id?: number
  outline_id?: number
  character_id?: number
  selection?: string
}

export interface AgentToolCall { id: string; name: string; arguments: string }
export interface AgentCheckpoint {
  tool_calls_used: number
  pending_change_ids: number[]
  assistant_message_id: number | null
}
export interface AgentConversation {
  id: number
  user_id: number
  project_id: number
  title: string
  auto_execute: boolean
  summary: string
  summarized_until: number | null
  last_message_at: string | null
  created_at: string
  updated_at: string
}
export interface AgentMessage {
  id: number
  conversation_id: number
  turn_task_id: number | null
  role: 'user' | 'assistant' | 'tool'
  content: string
  tool_calls: AgentToolCall[] | null
  tool_call_id: string | null
  tool_name: string | null
  page_context: AgentPageContext | null
  usage: Record<string, unknown> | null
  created_at: string
  updated_at: string
}
export type AgentChangeStatus = 'proposed' | 'executing' | 'executed' | 'rejected' | 'failed' | 'undone'
export interface AgentPreview {
  title: string
  target: unknown
  diff: { field: string; label: string; before: unknown; after: unknown }[]
  warnings: string[]
}
export interface AgentEffect {
  table: string
  pk: unknown
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
}
export interface AgentChange {
  id: number
  conversation_id: number
  project_id: number
  turn_task_id: number | null
  tool_call_id: string
  tool_name: string
  arguments: Record<string, unknown>
  kind: 'small' | 'delete' | 'task' | 'text'
  preview: AgentPreview
  status: AgentChangeStatus
  result: Record<string, unknown> | null
  effects: AgentEffect[]
  error: string
  executed_at: string | null
  undone_at: string | null
  seq: number
  created_at: string
  updated_at: string
}
export interface AgentConversationDetail extends AgentConversation {
  messages: AgentMessage[]
  changes: AgentChange[]
  active_task: Task | null
}
export interface AgentSendRequest { content: string; page_context?: AgentPageContext; ai_config_id?: number | null }
export interface AgentSendResult { message: AgentMessage; task: Task }
export interface AgentTurnResult { changes: AgentChange[]; task: Task | null; error?: string | null }
export interface AgentToolStart { call_id: string; name: string; label: string }
export interface AgentToolEnd { call_id: string; ok: boolean; summary: string }
