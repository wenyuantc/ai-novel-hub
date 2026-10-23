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
}

export interface ProjectCreate {
  title: string
  genre?: string
  premise?: string
  style?: string
}

export interface ProjectUpdate extends Partial<ProjectCreate> {
  status?: ProjectStatus
}

export interface WorldSetting {
  id: number
  project_id: number
  title: string
  content: string
  sort: number
}

export interface WorldSettingInput {
  title: string
  content?: string
  sort?: number | null
}

export interface Character {
  id: number
  project_id: number
  name: string
  role_type: string
  profile: string
  relations_text: string
  sort: number
}

export interface CharacterInput {
  name: string
  role_type?: string
  profile?: string
  relations_text?: string
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

export type WizardKind = 'outline' | 'characters' | 'world'

export interface WizardRequest {
  ai_config_id?: number | null
  instruction?: string
  volume_count?: number
  chapters_per_volume?: number
  character_count?: number
}

export interface WizardItemMap {
  outline: Outline
  characters: Character
  world: WorldSetting
}

export type VersionSource = 'manual' | 'baseline' | 'generate' | 'polish' | 'rewrite' | 'restore'

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

export type GenerationKind = 'generate' | 'polish' | 'rewrite' | 'expand' | 'condense' | 'deslop' | 'continue'

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

export type TaskKind = 'chapter.generate' | 'chapter.polish' | 'wizard.outline' | 'wizard.characters' | 'wizard.world'

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
