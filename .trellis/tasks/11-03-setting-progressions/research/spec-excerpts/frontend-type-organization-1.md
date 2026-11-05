# 任务上下文摘录：frontend-type-organization-1

来源：`.trellis/spec/frontend/type-safety.md`，原文第1–60行。
源文件SHA256：`23e1b75f6a6e760516cc4b33c95689b82cc4c82caafb370a9a1f17fb216040af`。
这是2026-11-03为避免自动注入截断而保留的连续原文，不修改或降低原规范；涉及摘录外主题时按原索引读取相应章节。若原规范更新，以原文件为准。

---

# 类型安全

RBAC传输镜像app/schemas/rbac.py：User必含is_super_admin/roles/effective_permissions:PermissionCode[]/authorization_revision，AdminUser必含is_super_admin/is_management_account/roles/authorization_revision但不含目标完整权限；不能将新字段改optional迁就旧fixture，或用is_admin fallback。[完整角色/配额API与错误契约](../backend/rbac-quotas.md)。QuotaConfig为inherit/unlimited/limited(value)判别联合，limited严格整数0..2147483647；revision（对象）与authorization_revision（全局）不能混用。

管理员系统设置镜像 `SystemSettingsOut/SystemSettingsUpdate` 与bool/int区分式items；该PATCH的null明确表示取消后台覆盖，不是忽略。审计settings_changes仅六项受限值，公开配置请求代次独立于登录态；详见[系统设置](../backend/system-settings.md)。

管理员任务的AdminTaskBrief/Detail/List/Counts、AdminTaskListQuery及AdminTaskCancelRequest/Out独立镜像schemas/admin_task.py，不能把普通Task通过强转复用；未知kind/status为unknown、旧入队时间/本轮时长为null。UUID深链比较规范化，原因按Unicode字符计数及控制字符规则校验；task.cancel_request审计新增专用有界字段，其成功只表示受理。详见[任务监控](../backend/admin-task-monitoring.md)。

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
  | `TaskKind` | `'agent.turn' \| 'chapter.generate' \| 'chapter.polish' \| 'wizard.outline' \| 'wizard.characters' \| 'wizard.world' \| 'character.generate' \| 'organization.generate' \| 'relations.parse' \| 'relations.generate' \| 'career.generate' \| 'wizard.careers' \| 'chapter.analyze' \| 'chapter.revise' \| 'outline.continue' \| 'outline.expand' \| 'chapter.batch' \| 'kb.index' \| 'book.import'` | `schemas/task.py` |
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

