// 与后端 app/schemas/*.py 对齐的类型定义

export interface User {
  id: number
  username: string
  created_at: string
}

export interface AuthConfig {
  registration_enabled: boolean
}

export interface Credentials {
  username: string
  password: string
}

export type ProviderName = 'openai_compat' | 'anthropic' | 'gemini'

/** off = 不发送思考参数 */
export type ThinkingLevel = 'off' | 'low' | 'medium' | 'high'

export interface AIConfig {
  id: number
  name: string
  provider: ProviderName
  base_url: string | null
  model: string
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
