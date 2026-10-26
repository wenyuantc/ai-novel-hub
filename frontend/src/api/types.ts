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
export type ModelType = 'text' | 'image'

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
  style: string
  status: ProjectStatus
  created_at: string
  updated_at: string
  chapter_count: number
  total_words: number
  /** AI 生成 / 润色 / 按建议重写成功后自动排队分析本章（默认 true） */
  auto_analyze: boolean
  /** 向导进度（outline-pro）：步骤名由前端定义（WIZARD_STEP_KEYS），后端不校验取值 */
  wizard_progress: WizardProgress
}

export interface WizardProgress {
  done: string[]
  skipped: string[]
  current: string | null
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
  style?: string
}

export interface ProjectUpdate extends Partial<ProjectCreate> {
  status?: ProjectStatus
  auto_analyze?: boolean
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
}

export interface WizardItemMap {
  outline: Outline
  characters: Character
  world: WorldSetting
  careers: Career
}

export type VersionSource = 'manual' | 'baseline' | 'generate' | 'polish' | 'rewrite' | 'restore' | 'revise'

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

export type TaskStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'

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
  /** true = 排队 + 运行中；false = 已结束；省略 = 全部 */
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
