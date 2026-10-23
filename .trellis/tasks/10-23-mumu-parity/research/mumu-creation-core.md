# Research: MuMuAINovel 创作核心流程功能调研

- **Query**: 调研 MuMuAINovel 创作核心流程（向导/灵感/大纲/章节/批量/重写/分析/风格/历史/后台任务）的功能、交互、数据模型、API、AI 调用方式与缺陷
- **Scope**: internal（外部仓库 `/Users/wenyuantc/IdeaProjects/my/MuMuAINovel` 源码阅读；GPL v3，仅调研功能与交互，不复制代码）
- **Date**: 2026-10-23
- 路径约定：下文 `B/` = `MuMuAINovel/backend/app/`，`F/` = `MuMuAINovel/frontend/src/`

---

## 0. 总览：创作主链路

```
灵感模式(对话式) ─┐
                  ├─> 项目向导 SSE：世界观 → 职业体系 → 角色 → 大纲(默认3节点)
表单向导        ─┘        (project.wizard_step 0..4 断点续跑)
                                   │
         outline_mode = one-to-one ┼─ 自动为每个大纲建 1 章
                       one-to-many ┴─ 大纲页手动“展开”→ AI 生成章节规划(expansion_plan) → 建多章
                                   │
大纲续写/全新生成(SSE 或后台任务)   │
                                   ▼
章节生成(单章 SSE / 单章后台 / 批量后台) ── 严格顺序：前置章节必须有内容 + 上一章分析必须完成
                                   │
                                   ▼
章节分析(异步 asyncio 任务) → PlotAnalysis + 记忆向量 + 伏笔 + 角色/职业/组织状态联动
                                   │
                                   ▼
根据建议重新生成(SSE，RegenerationTask) / 局部选段重写(SSE) → 对比 → 应用 → 自动重新分析
```

关键文件：

| 文件 | 说明 |
|---|---|
| `B/api/wizard_stream.py` (1789 行) | 向导 SSE：world-building / career-system / characters / outline / 世界观重生成 |
| `B/api/inspiration.py` (491 行) | 灵感模式：分步生成选项、按反馈重生成选项、智能补全 |
| `B/api/outlines.py` (3429 行) | 大纲 CRUD、生成/续写（SSE+后台）、展开（单个/批量，SSE+后台）、一对一建章 |
| `B/api/outline_transfer.py` + `B/services/outline_transfer_service.py` | 大纲 JSON 导出/导入预览/导入（append/merge） |
| `B/api/chapters.py` (5251 行) | 章节 CRUD、单章生成（SSE/后台）、分析、批量生成、重新生成、局部重写 |
| `B/api/polish.py` | “AI去味”（**未在 main.py 注册路由，前端路由也被注释**，属死代码） |
| `B/api/writing_styles.py` | 写作风格 CRUD、项目默认风格 |
| `B/api/tasks.py` + `B/services/background_task_service.py` | 统一任务面板 API；按用户 FIFO 队列的后台任务执行器 |
| `B/services/plot_expansion_service.py` | 大纲展开为章节规划（分批、去重关键事件） |
| `B/services/chapter_context_service.py` (1913 行) | 1-1 / 1-N 两套章节上下文构建器 |
| `B/services/chapter_regenerator.py` | 重新生成：修改指令拼装、diff 统计 |
| `B/services/prompt_service.py` | 全部系统默认提示词模板 + 用户自定义模板覆盖 |
| `B/main.py:60-105` | 启动时将 pending/running 的三类任务统一标记为 failed（“服务重启，任务已中断”） |
| `F/pages/Inspiration.tsx`、`F/pages/ProjectWizardNew.tsx`、`F/components/AIProjectGenerator.tsx` | 灵感对话、向导表单、向导执行与断点续跑 |
| `F/pages/Outline.tsx` (2570 行) | 大纲管理页 |
| `F/pages/Chapters.tsx` (3170 行) | 章节管理页（生成、批量、规划查看/编辑、局部重写） |
| `F/pages/ChapterAnalysis.tsx`、`F/components/ChapterAnalysis.tsx` | 剧情分析页/分析弹窗（含“根据建议重新生成”） |
| `F/pages/ChapterReader.tsx`、`F/components/ChapterReader.tsx` | 阅读器（主题/字号/翻页；带标注） |
| `F/components/ChapterRegenerationModal.tsx`、`ChapterContentComparison.tsx`、`PartialRegenerateModal.tsx`、`PartialRegenerateToolbar.tsx`、`ExpansionPlanEditor.tsx`、`FloatingTaskPanel.tsx` | 重写/对比/局部重写/规划编辑/任务浮窗 |
| `F/pages/WritingStyles.tsx`、`F/pages/ProjectDetail.tsx` | 写作风格页、项目详情外壳（侧边菜单 + 统计） |

---

## 1. 灵感模式（对话式立项）

**用户可见行为**（`F/pages/Inspiration.tsx`）
- 聊天界面。步骤：`idea → title → description → theme → genre → perspective → outline_mode → confirm → generating → complete`（`Inspiration.tsx:11`、`:380`）。
- 用户先输入一句想法；AI 为“书名/简介/主题/类型”各生成约 6 个选项卡片，用户点选（类型为多选），也可以对某条 AI 消息输入反馈“我想要更悲剧一些”让 AI 重新生成该步选项。
- 叙事视角、大纲模式（一对一/一对多）为固定选项。确认后直接进入向导执行组件 `AIProjectGenerator`，参数写死 `target_words=100000, chapter_count=3, character_count=5`（`Inspiration.tsx:578-589`）。
- 对话进度自动缓存到 localStorage（只在对话阶段，消息数>1 时保存），失败请求可重试（`lastFailedRequest`）。

**API**
- `POST /api/inspiration/generate-options` `{step, context}` → `{prompt, options[]}`
- `POST /api/inspiration/refine-options` `{step, context, feedback, previous_options}`
- `POST /api/inspiration/quick-generate` 已有字段 → AI 补全缺失的 title/description/theme/genre（用户输入优先）

**AI 调用方式**
- 每步独立 SYSTEM/USER 模板：`INSPIRATION_{TITLE|DESCRIPTION|THEME|GENRE}_{SYSTEM|USER}`（`B/services/prompt_service.py:1669-1752`），可被用户自定义模板覆盖。
- 温度随步骤递减：title 0.8 / description 0.65 / theme 0.55 / genre 0.45（`inspiration.py:18-24`），保证越往后越贴合前文。
- 非流式返回 JSON；后端 `validate_options_response` 校验 3–10 个选项、每项 ≤500 字、genre 每项 ≤10 字，失败自动重试 3 次（`inspiration.py:27-67`）。
- quick-generate 内部用流式累积后解析 JSON，无重试。

**缺陷 / 可改进点**
- 对话状态只存在浏览器 localStorage，换设备/清缓存即丢；后端无“灵感会话”实体。
- 确认后目标字数、大纲节点数、角色数写死，用户无法在灵感模式里调整。
- 每次 refine 不带完整多轮历史，仅带 feedback + previous_options，长对话的偏好无法累积。
- 选项只是字符串，不能对单个选项“微调/合并”，也不能手动编辑后再继续。
- quick-generate 解析失败直接报错，无重试，与 generate-options 行为不一致。

---

## 2. 项目创建向导（表单 + SSE 分阶段生成 + 断点续跑）

**用户可见行为**
- 表单（`F/pages/ProjectWizardNew.tsx`）：书名、简介、主题、类型、大纲章节模式（一对一“传统模式” / 一对多“细化模式”，Radio 卡片）、叙事视角、角色数量、目标字数。
- 提交后进入 `AIProjectGenerator`（`F/components/AIProjectGenerator.tsx`），展示 4 个步骤进度条：生成世界观 → 生成职业体系 → 生成角色 → 生成大纲（`:1080-1084`），每步实时显示字数/重试次数。
- 每一步失败可“从该步重试”（`retryFromWorldBuilding/Careers/Characters/Outline`）。
- 断点续跑：URL 带 `?project_id=` 时读取 `project.wizard_step`（0 未开始 / 1 世界观完成 / 2 职业完成 / 3 角色完成 / 4 大纲完成）从下一步继续（`AIProjectGenerator.tsx:188-225`）；进度也存 localStorage。
- 世界观可单独“重新生成”（`POST /wizard-stream/world-building/{project_id}/regenerate`）。

**API（均为 SSE，前缀 `/api/wizard-stream`）**
- `POST /world-building`：创建 Project 记录（`wizard_step=1`、`outline_mode`），写入世界观四字段（time_period/location/atmosphere/rules）。
- `POST /career-system`：生成主/副职业体系，`wizard_step=2`。
- `POST /characters`：分批生成角色（每批 5 个，每批最多重试 3 次），并按职业体系为角色分配 `career_assignment`（主职业 1 个 + 副职业 0–2 个），`wizard_step=3`。
- `POST /outline`：生成“开局”大纲节点（默认 3 个，提示词强调“这是开局，不要完结故事”），之后自动校验大纲中出现但不存在的角色/组织并 AI 自动创建；一对一模式自动建章；`wizard_step=4`、`wizard_status=completed`、`status=writing`。

**AI 调用方式**
- 模板：`WORLD_BUILDING`、`CAREER_SYSTEM_GENERATION`、`CHARACTERS_BATCH_GENERATION`、`OUTLINE_CREATE`。
- 统一 `WizardProgressTracker` 输出 SSE 阶段事件（start/loading/preparing/generating/parsing/saving/complete/result/error/retry/heartbeat）。
- 每步 “流式生成 → JSON 清洗解析 → 失败重试 3 次”；可注入知识库上下文（`knowledge_service.get_generation_context`）与 MCP 工具（`enable_mcp` 默认 true）。

**缺陷 / 可改进点**
- 一对一模式下“向导建章”`outline_id=None`、靠 `chapter_number == outline.order_index` 匹配（`wizard_stream.py:1473-1490`），而大纲页生成的一对一章节却设置了 `outline_id`（`outlines.py:_save_outlines`），**两套关联方式不一致**，后续“改大纲标题同步章节/删除大纲删章节”都依赖序号匹配，插入/重排大纲后容易错位。
- 向导的 `chapter_count` 实际是“大纲节点数”，最后又把 `project.chapter_count` 覆写为“实际创建章节数”，一对多模式下变成 0，语义混乱。
- SSE 断连 = 当前步骤丢失（生成器 GeneratorExit 回滚），只能依赖 wizard_step 粗粒度重跑整步；向导没有走后台任务机制。
- 每一步都是“全有或全无”，无法在向导中预览/编辑某步结果后再继续（例如先改世界观再生成角色）。
- 进度估算为固定字数（如世界观 1000 字），进度条不准确。

---

## 3. 大纲管理（生成 / 续写 / 编辑 / 导入导出）

**数据模型**（`B/models/outline.py`）
- `Outline{id, project_id, title, content, structure(Text-JSON), order_index, created_at, updated_at}`
- `structure` JSON 典型字段：`title, summary/content, key_points[], characters[{name,type:character|organization}], scenes, emotion, goal`（续写上下文从中提取，`outlines.py:424-520`）。
- 项目级 `Project.outline_mode ∈ {one-to-one, one-to-many}`，创建后固定。

**用户可见行为**（`F/pages/Outline.tsx`）
- 顶部按钮：导入、导出、手动新建大纲、AI 生成、（一对多）批量展开。
- “AI 生成”弹窗：生成模式 `自动判断 / 全新生成 / 续写模式`；全新生成填主题、章节数、叙事视角、其他要求；续写填故事发展方向、情节阶段（development/climax/ending）；可选 AI 模型。
- 大纲卡片：编辑（标题、内容、涉及角色、涉及组织、场景信息、情节要点、情感基调、叙事目标——即编辑 structure）、删除、（一对多）展开。
- 导入：上传 JSON → 预览统计（将新建 / 将更新 / 将新建章节、错误、警告）→ 选择 `append`（追加并从末尾重新编号）或 `merge`（按 order_index 覆盖）→ 导入。兼容“完整项目导出文件”只取大纲部分。

**API**
- CRUD：`POST/GET /api/outlines`、`GET /outlines/project/{pid}`、`GET/PUT/DELETE /outlines/{id}`
- 生成：`POST /outlines/generate`（后台任务，返回 task_id，`task_type=outline_new|outline_continue`）、`POST /outlines/generate-stream`（SSE）
- 导入导出：`POST /outline-transfer/export`、`.../preview`、`.../import`

**AI 调用方式**
- 全新：`OUTLINE_CREATE` 模板，参数含世界观、角色摘要、叙事视角、requirements + 知识库上下文；一次生成全部。
- 续写：`OUTLINE_CONTINUE`，**分批每 5 章**生成；上下文 = 项目基础信息 + 最近 10 章 structure 文本化 + 全部角色 + 伏笔提醒/伏笔统计（已埋/已回收/部分回收/待埋）+ plot_stage 指导语 + story_direction。
- 生成后自动检查大纲中提到但不存在的角色/组织 → AI 自动创建（`_check_and_create_missing_characters/organizations_from_outlines`）。
- `_normalize_outline_data` / `_parse_ai_response` 负责清洗 JSON 与补齐序号。

**副作用（重要）**
- “全新生成”会**删除项目全部旧大纲、全部章节、分析记录、向量记忆、分析来源伏笔并重置手动伏笔**，并扣减项目字数（`outlines.py` new 模式，约 1208-1290 行）。
- PUT 大纲：未显式传 structure 时，title/content 会同步写回 structure；一对一模式下按 `chapter_number == order_index` 同步章节标题。
- DELETE 大纲：同时删除对应章节（一对一按序号、一对多按 outline_id）及其伏笔/记忆。

**缺陷 / 可改进点**
- “全新生成”是破坏性操作且不可恢复（无快照/回收站），仅前端确认。
- 大纲没有版本历史；AI 续写结果直接落库，无“预览→挑选→采纳”步骤。
- 大纲没有层级（卷/幕/章），只有扁平 order_index；没有拖拽重排接口（重排会破坏一对一按序号的关联）。
- structure 是自由 JSON 文本，没有 schema 校验，前后端字段名有新旧兼容分支（characters 新旧格式）。
- 后台任务版与 SSE 版是两份几乎重复的实现（`_run_new_outline_bg` vs `new_outline_generator`），行为容易漂移。

---

## 4. 大纲 → 章节：一对一 / 一对多模式与展开

**数据模型**（`B/models/chapter.py`）
- `Chapter{id, project_id, chapter_number, title, content, summary, word_count, status(draft/pending/completed…), outline_id(FK SET NULL), sub_index, expansion_plan(Text-JSON), created_at, updated_at}`
- `expansion_plan` JSON：`key_events[], character_focus[], emotional_tone, narrative_goal, conflict_type, estimated_words(默认3000), scenes[]|null`（`plot_expansion_service.py:446-452`）。

**一对一（传统模式）**
- 每个大纲 = 一章；大纲生成/导入时自动建章（`status=pending`, `summary=大纲内容`）。
- 也可手动 `POST /outlines/{id}/create-single-chapter`（不调 AI；检查同序号章节是否已存在）。
- 章节生成时的“本章大纲”来自 outline.structure；角色筛选来自 structure.characters。

**一对多（细化模式）**
- 用户在大纲卡片点“展开”：弹窗选择 目标章节数、展开策略（`balanced 均衡分配 / climax 高潮重点 / detail 细节丰富`）、是否场景级分析。
- 若该大纲已有展开章节，先显示“已有规划预览”，可选择“删除所有展开的章节”（前端**顺序**逐个删除以避免并发字数计算竞态，`Outline.tsx:~1067`）后重新展开。
- 展开流程：AI 生成章节规划（plans）→ 前端展示每章卡片（基本信息/情节概要/叙事目标/关键事件/涉及角色/场景）→ 确认后 `POST /outlines/{id}/create-chapters-from-plans` 落库；或 `auto_create_chapters=true` 一步建章。
- 批量展开：`POST /outlines/batch-expand-stream|batch-expand-background`，参数 `chapters_per_outline`、`expansion_strategy`、`outline_ids`，已展开的大纲会被跳过（`skipped_outlines`）。
- 起始章节号自动计算 = 前序所有大纲已展开章节数 + 1，建章后 `_renumber_subsequent_chapters` 重排后续章节号。
- 章节页可查看/编辑规划：`PUT /chapters/{id}/expansion-plan`（`F/components/ExpansionPlanEditor.tsx`）。

**AI 调用方式**（`B/services/plot_expansion_service.py`）
- ≤ batch_size(默认5) 用 `OUTLINE_EXPAND_SINGLE`；超过则 `OUTLINE_EXPAND_MULTI` 分批，每批提示词附带“已生成章节摘要 + 已使用关键事件列表”，要求新章节 key_events 不得重复（`:189-236`）。
- 上下文含前一个/后一个大纲各 200 字（`_get_outline_context`）。
- 解析后根据 narrative_goal 关键词推断 conflict 类型等（`_parse_expansion_response`）。

**缺陷 / 可改进点**
- 模式是项目级二选一，不能“某些大纲一对一、某些一对多”，也无法在项目中途切换模式。
- 一对一关联混用 outline_id 与序号匹配（见 §2），是脆弱点。
- 展开规划只存在章节上，“未建章的规划预览”不持久化（SSE 断开即丢；后台版本存在 task_result 里）。
- 重新展开必须先手动删光已有章节（含正文），无“保留已写章节、只补规划”的增量模式。
- 章节重排依赖全量 renumber，没有并发保护。

---

## 5. 章节生成（单章 SSE / 单章后台）与上下文工程

**用户可见行为**（`F/pages/Chapters.tsx`）
- 章节列表（一对多时按大纲分组），每章：编辑、AI 创作、查看展开详情、删除、阅读、分析。
- 点“AI 创作”打开编辑弹窗：写作风格（**必选**）、叙事角度（可临时覆盖）、应用 Skill、目标字数（InputNumber 500–10000，步长 100，值缓存在 localStorage）、AI 模型；确认弹窗列出将参考的前置章节，并警告“此操作将覆盖当前章节内容”。
- 生成中正文实时流入编辑器；生成中禁止关闭弹窗。也可“后台生成”，提交后在右下角任务浮窗看进度。
- 生成前置门槛：**所有序号更小的章节必须有内容**（`check_prerequisites`，`chapters.py:509`）且**上一章的最新分析任务必须 completed**（`check_previous_analysis_ready`，`:547`，否则 409）。`GET /chapters/{id}/can-generate` 给前端判断按钮可用性与原因。

**API**
- `POST /chapters/{id}/generate-stream`（SSE），请求体 `ChapterGenerateRequest{style_id, target_word_count(默认3000,500-10000), model, narrative_perspective, skill_key, enable_mcp}`
- `POST /chapters/{id}/generate-background`（BackgroundTask，`task_type=chapter_generate`，生成后在同任务中执行分析并映射到 94–99% 进度）
- `GET /chapters/{id}/navigation`（上一章/下一章）

**AI 调用方式**
- 4 个模板按 “模式 × 是否首章” 选择：`CHAPTER_GENERATION_ONE_TO_ONE`、`..._ONE_TO_ONE_NEXT`、`..._ONE_TO_MANY`、`..._ONE_TO_MANY_NEXT`（`prompt_service.py:560-866`）。模板为 XML 分区并标注优先级：`<outline P0>`、`<characters P1>`、`<careers P2>`、`<foreshadow_reminders P2>`、`<memory P2>`、`<constraints>`（禁止输出标题、禁止“总之/综上所述”、禁止结尾反问等）。
- 上下文构建（`B/services/chapter_context_service.py`）：
  - 1-N（`OneToManyContextBuilder`）：本章 expansion_plan + 最近 10 章规划/摘要 + **衔接锚点（上一章完整正文 + 摘要）** + 本章 character_focus 筛选的角色（≤10，含职业阶段、组织职位、当前状态）+ 伏笔提醒 + 基于规划构造查询的向量记忆。
  - 1-1（`OneToOneContextBuilder`）：outline.structure + 最近章节摘要 + 上一章完整正文 + structure.characters 角色 + 本章职业 + 伏笔 + 记忆。
  - 均可追加知识库上下文。
- 写作风格注入**系统提示词**（“写作风格要求 - 最高优先级”）；有 Skill 时 Skill 工作流在前、风格作为“补充”。
- `max_tokens = clamp(target_word_count × 3, 2000, 16000)`。
- 流式：每 chunk 推送内容，每 5 chunk 推进度，每 20 chunk 心跳。
- 保存：`content`、`word_count = len(full_content)`、`status=completed`、`summary = 前 300 字轻量摘要`、项目 `current_words` 增量更新、写 `GenerationHistory`、自动把“待埋入”伏笔标记为已埋（`auto_plant_pending_foreshadows`），然后创建 `AnalysisTask` 并异步启动分析。

**字数控制**
- 仅靠提示词“目标字数 X 字（允许 ±200 字浮动）”+ max_tokens 上限；上下文中另有 `min_word_count = max(500, target-500)`、`max_word_count = target+1000`。
- 生成后**不校验、不续写、不截断**；`word_count` 用 `len()`，含标点、空白与换行。
- 前端单章与批量的目标字数上限都是 10000。

**缺陷 / 可改进点**
- 字数统计口径粗糙（字符数≠中文字数），无“字数不足自动续写/超出自动收尾”的闭环。
- 严格串行门槛：上一章分析失败/卡住会阻塞后续所有生成，且不允许跳章或并行草拟。
- 覆盖生成不保留旧正文（单章再生成直接覆盖，只有“重新生成”流程才存原文快照）。
- `chapters.py` 中 `_run_chapter_generation_bg` **定义了两次**（`:2079` 与 `:2591` legacy 版本），Python 以后者为准，导致前一版的“保存前检查取消”逻辑实际失效；`_build_analysis_task_status_payload` 同样重复定义。
- SSE 版与后台版、批量版是三份相似实现，提示词/上下文选择逻辑重复（批量 `generate_single_chapter_for_batch` 又是一份）。
- 前 300 字“摘要”不是真正摘要，后续“最近章节摘要”上下文质量受限（直到分析完成才有更好的 plot_points）。

---

## 6. 批量生成

**数据模型**（`B/models/batch_generation_task.py`）
- `BatchGenerationTask{id, project_id, user_id, start_chapter_number, chapter_count, chapter_ids(JSON), style_id, target_word_count(默认3000), enable_analysis, status(pending/running/completed/failed/cancelled), total_chapters, completed_chapters, failed_chapters(JSON[]), current_chapter_id, current_chapter_number, current_retry_count, max_retries(默认3), created_at, started_at, completed_at, error_message(500)}`

**用户可见行为**
- “批量生成”按钮：自动定位第一个无内容章节作为默认起点（若其不可生成则提示原因）。
- 弹窗：起始章节（下拉）、生成数量（**固定 5/10/15/20 章**）、写作风格（必选）、目标字数、AI 模型、Skill；“同步分析”固定开启且禁用（提示“必须开启，确保剧情连贯”）。
- 提交后提示预计耗时（每 3000 字约 2 分钟 + 分析 1 分钟/章），可切页；重开页面自动检测运行中批量任务并恢复轮询；可取消。

**API**
- `POST /chapters/project/{pid}/batch-generate` → `{batch_id, chapters_to_generate[], estimated_time_minutes}`
- `GET /chapters/batch-generate/{batch_id}/status`、`GET /chapters/project/{pid}/batch-generate/active`、`POST /chapters/batch-generate/{batch_id}/cancel`

**执行机制**（`execute_batch_generation_in_order`，`chapters.py:3829`）
- 由 **FastAPI `BackgroundTasks`** 启动（不进入 BackgroundTaskService 的用户队列）。
- 严格按 chapter_ids 顺序：每章 生成 → 同步分析（`enable_analysis` 强制 true）→ 下一章；每章前检查取消、检查上一章分析就绪。
- 单章失败重试 `max_retries` 次，指数退避 `min(2^n, 10)` 秒；仍失败或分析失败 → 整个任务 `failed` 并停止，失败信息写入 `failed_chapters`。

**缺陷 / 可改进点**
- 选定范围内**已有内容的章节也会被重新生成覆盖**（范围按序号筛选，未跳过已写章节）。
- 数量只能选 5/10/15/20，不能自定义或“生成到某章为止”。
- 批量任务不走统一用户队列，可能与同用户的后台单章/大纲任务并发写同一项目。
- 失败即终止，无“跳过失败章继续”“从失败处续跑”按钮（需重新发起）。
- 无章间人工审核点（如每 N 章暂停确认）。
- 服务重启后统一标记 failed，不可自动续跑。

---

## 7. 章节分析（剧情分析）

**数据模型**
- `AnalysisTask{id, chapter_id(FK CASCADE), user_id, project_id, status(pending/running/completed/failed), progress 0-100, error_message, created_at, started_at, completed_at, archived_at}`；索引 `(chapter_id, created_at)`、`status`、任务面板索引（`B/models/analysis_task.py`）。
- `PlotAnalysis`（`B/models/memory.py`，chapter_id 唯一）：`plot_stage, conflict_level(1-10), conflict_types[], emotional_tone, emotional_intensity, emotional_curve{start,middle,end}, hooks[], hooks_count, hooks_avg_strength, foreshadows[], foreshadows_planted, foreshadows_resolved, plot_points[], plot_points_count, character_states[], scenes[], pacing, overall_quality_score, pacing_score, engagement_score, coherence_score, analysis_report, suggestions[], word_count, dialogue_ratio, description_ratio`。

**用户可见行为**
- 生成后自动分析；也可手动“分析”、或“一键分析未分析章节”（按章节顺序逐个执行）。
- 分析弹窗（`F/components/ChapterAnalysis.tsx`）Tabs：概览（整体/节奏/吸引力/连贯性 四项评分、分析摘要、实体联动更新统计：职业更新/角色状态关系更新/组织状态更新、**“根据建议重新生成”按钮**）、钩子、伏笔、情感曲线、角色、记忆。
- 剧情分析页 / 阅读器（`F/pages/ChapterAnalysis.tsx`、`F/pages/ChapterReader.tsx`）：正文上做**可视化标注**（钩子🎣/伏笔🌟/情节点💎/角色事件👤），侧栏 `MemorySidebar` 联动定位。

**API**
- `POST /chapters/{id}/analyze`、`GET /chapters/{id}/analysis`、`GET /chapters/{id}/analysis/status`、`POST /chapters/project/{pid}/analysis/statuses`（批量查状态）、`POST /chapters/project/{pid}/analysis/analyze-unanalyzed`、`GET /chapters/{id}/annotations`

**AI 调用方式与联动**（`_analyze_chapter_background_impl`，`chapters.py:925`）
- `asyncio.create_task` 后台执行（不走用户队列），按用户加写锁保护 DB 写入；超时 600s、陈旧判定 720s 并有恢复逻辑（`_recover_stale_analysis_tasks`）。
- 模板 `PLOT_ANALYSIS`（`prompt_service.py:1047`）：传入 已埋入伏笔列表（带 ID，要求回收时填 `reference_foreshadow_id`）、本章相关角色（1-N 用 character_focus、1-1 用 structure.characters 筛选）。分析维度：钩子（类型/强度/位置/**8–25 字原文关键词用于定位**）、伏笔（埋下/回收、隐藏度、长线、预估回收章）、冲突、情感曲线、角色状态、组织状态、情节点、场景与节奏、**质量评分（1.0–10.0 带严格分档标准，overall=三项平均）**、改进建议（数量与分数挂钩：<4 分 4–5 条…≥8 分 0–1 条）。
- 落库后：清理该章旧的分析伏笔 → 写伏笔；删旧记忆 → 抽取记忆写 `StoryMemory` + 向量库；根据 character_states 更新角色职业阶段、角色状态/关系、组织状态。
- 修改章节正文（PUT）时会删除该章分析任务、PlotAnalysis、记忆、分析来源伏笔。

**缺陷 / 可改进点**
- 分析结果只保留最新一份（chapter_id 唯一），无历史对比，无法看到“重写前后评分变化”。
- 评分完全由 LLM 自评，没有规则/统计指标兜底（dialogue_ratio 等也是 LLM 给的）。
- 分析任务不进入统一队列，大量章节同时触发时并发调用 AI。
- 分析失败会阻塞后续章节生成（见 §5）。
- 标注定位依赖“原文逐字关键词”，LLM 复述不准时标注丢失。

---

## 8. 重新生成（根据分析建议一键重写）

**数据模型**（`B/models/regeneration_task.py`）
- `RegenerationTask{id, chapter_id, analysis_id, user_id, project_id, modification_instructions, original_suggestions(JSON), selected_suggestion_indices(JSON), custom_instructions, style_id, target_word_count, focus_areas(JSON), preserve_elements(JSON), status, progress, error_message, original_content(快照), original_word_count, regenerated_content, regenerated_word_count, version_number(默认1), version_note, created_at, started_at, completed_at}`

**用户可见行为**（`ChapterRegenerationModal.tsx` → `ChapterContentComparison.tsx`）
- 修改来源：自定义 / 仅分析建议 / 混合（有分析建议时默认混合）。
- 勾选分析建议（显示优先级颜色与类别）、填写自定义修改要求、重点优化方向多选（节奏 pacing / 情感 emotion / 场景描写 description / 对话 dialogue / 冲突 conflict）、保留元素（保持整体结构、保留指定关键对话、保留指定情节点、保持角色性格，默认勾选后者）、风格、目标字数、版本说明。
- 流式生成新内容 → 打开左右对比视图（字数变化、相似度等 diff 统计）→ “应用新内容”= 前端 `PUT /chapters/{id}` 覆盖正文，500ms 后自动 `POST /analyze` 重新分析。

**API**
- `POST /chapters/{id}/regenerate-stream`（SSE），请求 `ChapterRegenerateRequest{modification_source, selected_suggestion_indices, custom_instructions, preserve_elements, style_id, target_word_count(500-10000), focus_areas, save_as_version, version_note, auto_apply}`
- `GET /chapters/{id}/regeneration/tasks`（版本列表）

**AI 调用方式**
- `ChapterRegenerator._build_modification_instructions` 拼成 Markdown 指令：分析建议 → 用户要求 → 优化方向 → 必须保留元素（`B/services/chapter_regenerator.py:110-179`）。
- `PromptService.get_chapter_regeneration_prompt` + `CHAPTER_REGENERATION_SYSTEM`：传入原文全文、修改指令、项目上下文（含按模式筛选的角色）、风格、目标字数（提示“±20%”）。
- `calculate_content_diff`：difflib 相似度、长度变化、段落数。

**缺陷 / 可改进点（已核实）**
- `auto_apply` 与 `save_as_version` 参数**未实现**：后端只回显 `auto_applied`，从不写回章节正文（`chapters.py:4757`）。
- `version_number` 从不递增，所有任务都是 1。
- `GET /regeneration/tasks` 在前端 `api.ts:795` 有封装但**没有任何页面调用**——没有版本历史 UI，也无法回滚到旧版本。
- 应用新内容走通用 PUT，会清空该章分析/记忆/伏笔再重新分析；不检查原文在重写期间是否被改过（无乐观锁）。
- 重写不在后台任务体系中，SSE 断开即丢。
- 对比视图仅整体左右对比，没有段落级 diff 或部分采纳。

---

## 9. 局部重写（选段改写）

**用户可见行为**：在章节编辑器中选中一段文字 → 浮出 `PartialRegenerateToolbar` → 弹窗填写修改要求、长度模式（保持长度 / 扩展内容 / 精简内容 / 自定义字数）→ 流式生成 → 预览后“应用”。

**API**
- `POST /chapters/{id}/partial-regenerate-stream`：`PartialRegenerateRequest{selected_text, start_position, end_position, user_instructions(1-1000), context_chars(默认500, 100-2000), style_id, length_mode, target_word_count}`；只返回结果，不保存。
- `POST /chapters/{id}/apply-partial-regenerate`：`{new_text, start_position, end_position}` 按字符索引拼接替换并更新字数。

**AI 调用**：模板 `PARTIAL_REGENERATE`（`prompt_service.py:2414`）：前文参考 / 需改写原文（含字数）/ 后文参考 + 用户要求 + 字数要求 + 风格；强调不重复前后文、可直接替换。

**缺陷 / 可改进点**
- 应用时只校验位置范围，**不校验该位置的原文是否仍等于 selected_text**，并发编辑后可能替换错位。
- 应用后不触发重新分析，也不清理过期标注/记忆；无局部改写历史与撤销。

---

## 10. 写作风格

**数据模型**
- `WritingStyle{id(自增), user_id(NULL=全局预设), name, style_type(preset/custom), preset_id(natural/classical/modern/literary/suspense/humorous), description, prompt_content, order_index, created_at, updated_at}`
- `ProjectDefaultStyle{id, project_id(唯一), style_id, created_at, updated_at}`——每项目一个默认风格。
- 预设 6 种由 Alembic 种子数据写入：自然流畅、古典优雅、现代简约、文艺细腻、紧张悬疑、幽默诙谐。

**用户可见行为**（`F/pages/WritingStyles.tsx`）：列表（预设 + 我的自定义），创建/编辑（名称、描述、提示词内容）、删除（预设不可删；被项目设为默认的不可删）、设为当前项目默认。章节生成/批量生成弹窗必须选择风格（默认选中项目默认风格）。

**API**：`GET /writing-styles/presets/list`、`POST /writing-styles`、`GET /writing-styles/user`、`GET /writing-styles/project/{pid}`（预设+自定义，标记 is_default）、`GET/PUT/DELETE /writing-styles/{id}`、`POST /writing-styles/{id}/set-default`、`POST /writing-styles/project/{pid}/init-defaults`

**AI 使用**：风格 `prompt_content` 原样注入系统提示词（最高优先级），用于章节生成、批量、重新生成、局部重写。

**缺陷 / 可改进点**
- 风格只是一段自由文本提示词，无结构化维度（句长、对白比例、描写密度、用词禁忌、示例段落等）。
- 不支持“从样文提取风格”（AI 学习用户上传片段）。
- 风格只到项目级默认，不能按章节/卷/角色视角设置；选择记录不随章节保存（只在 Batch/Regeneration 任务里记 style_id）。
- 风格效果无评估/预览（无“用此风格试写一段”）。

---

## 11. 生成历史

**数据模型**（`B/models/generation_history.py`）：`GenerationHistory{id, project_id(CASCADE), chapter_id(SET NULL), prompt, generated_content, model, tokens_used, generation_time, created_at}`

**现状**
- 章节生成/批量/后台、大纲生成、角色/组织生成会写一条；但 `prompt` 只是“创作章节: 第N章 标题”这类描述，`generated_content` 只截取前 500 字，`model="default"`，`tokens_used/generation_time` 未填（`chapters.py:1852-1859`）。
- **没有任何查询 API 与前端页面**；只在项目导出（`include_generation_history`）和删除项目时用到。
- `polish.py` 写入了模型中不存在的字段（`generation_type/result/provider`），且该路由未注册、`AI_DENOISING` 系统模板不存在——“AI 去味”功能实际不可用。

**可改进点**：完整记录 prompt/参数/模型/耗时/token/成本；提供按章节的历史版本列表、对比与回滚；与重新生成版本统一为“章节版本”体系。

---

## 12. 后台任务机制

**三套并存的任务实体**
| 实体 | 用途 | 执行方式 |
|---|---|---|
| `BackgroundTask` | 大纲生成/续写/展开/批量展开、单章后台生成、向导(预留) 等，`task_type` 字符串区分 | `BackgroundTaskService`：**每用户一个 asyncio.Queue + worker 协程，同用户 FIFO 串行、不同用户并发**；入队后写“排队中，前方还有 N 个任务” |
| `BatchGenerationTask` | 章节批量生成 | FastAPI `BackgroundTasks` 直接执行 |
| `AnalysisTask` | 章节分析 | `asyncio.create_task`（持有引用集合），按用户写锁 |
| `RegenerationTask` | 重新生成记录 | 仅 SSE 同步执行，记录用途 |

**BackgroundTask 字段**：`id, user_id, project_id, task_type, status, progress, status_message, task_input(JSON), task_result(JSON), error_message, progress_details(JSON{stage,message,current_chars,...}), cancel_requested, retry_count, max_retries, created_at, started_at, completed_at, updated_at`

**进度协议**（`TaskProgressTracker`，与 SSE 的 `WizardProgressTracker` 同构）：start 0 → loading 5–15 → preparing 17 → generating 20–85（按字数，单调不回退）→ parsing 88 → saving 92–98 → analyzing 94–99 → complete 100；另有 error/warning/retry/check_cancelled。每次更新都新开 DB 会话写库；任务已取消时跳过更新。

**统一任务 API**（`B/api/tasks.py`）
- `GET /api/tasks/{id}`：依次在三张表查找。
- `GET /api/tasks?project_id&task_type&limit`：合并三类任务，进行中优先、按创建时间倒序。
- `POST /api/tasks/{id}/cancel`：BackgroundTask 置 `cancel_requested + cancelled`；Batch 置 cancelled；分析任务不支持取消。
- `DELETE /api/tasks/{id}`：进行中不可删；分析任务改为归档（`archived_at`），因为章节分析状态依赖其最新记录。
- `DELETE /api/tasks/project/{pid}/clear`：清理已结束任务。
- 前端 `FloatingTaskPanel` 右下角浮窗轮询：有活动任务时按 autoRefreshInterval，否则 ≥15s。

**重启恢复**：`main.py:60-105` 启动时把三类 pending/running 任务全部置 failed（“服务重启，任务已中断，请重新发起”）。

**缺陷 / 可改进点**
- 队列在进程内存中，无持久化、无多实例支持，重启即全部失败且不能自动恢复/续跑。
- 取消是“软取消”：只置标志，生成中的 AI 流仅在检查点才停止；排队任务在出队时跳过。
- 三类任务执行路径不同，用户级串行只对 BackgroundTask 生效，批量/分析绕过队列，可能对同一项目产生并发写入与 AI 并发。
- 无任务优先级、无全局并发/速率限制、无失败自动重试策略统一配置（各处各写 3 次）。
- 任务结果（如展开规划）存在 task_result JSON 中，无结构化读取 API 与过期清理策略（`cleanup_old_tasks` 有实现但未见定时调用）。
- RegenerationTask 不出现在任务面板。

---

## 13. ProjectDetail（项目外壳）

- `F/pages/ProjectDetail.tsx`：左侧菜单分组“创作管理”（世界设定、角色、组织、职业、关系、大纲、章节、剧情分析、伏笔、知识库）与“创作工具”（写作风格、提示词工坊、Skill 工具箱、Skill 管理）；顶部统计 大纲数 / 角色数 / 章节数 / 已写字数（`current_words`）。
- “AI 去味”菜单项与路由均被注释（`ProjectDetail.tsx:341`、`App.tsx:77`）。

---

## 14. 横向总结：可“做得更完善”的方向（供 PRD 参考）

1. **统一任务引擎**：所有长耗时 AI 操作（向导、大纲、展开、单章、批量、分析、重写）走同一持久化队列；支持重启续跑、断点、优先级、并发/速率限制、真取消。
2. **统一章节版本体系**：每次生成/重写/局部改写/手动编辑都形成版本（完整 prompt、参数、模型、token、耗时），可对比、段落级采纳、回滚；修复“auto_apply/版本号未实现”的问题。
3. **大纲—章节关联统一用外键**：一对一也用 outline_id，不靠序号；允许按大纲粒度选择 1:1 或 1:N，支持重排与增量展开（保留已写章节）。
4. **字数闭环**：中文字数口径统计；不足自动续写、超出自动收尾；批量中按章节 `estimated_words` 动态目标。
5. **批量生成**：跳过已写章节选项、自定义范围、失败跳过/续跑、每 N 章人工审核点、与分析串行但可配置。
6. **分析**：保留分析历史，重写前后评分对比；规则统计指标（真实对白占比、段落长度等）与 LLM 评分结合；分析失败不阻塞生成（可降级）。
7. **破坏性操作保护**：“全新生成大纲”“重新展开”等改为软删除/快照，可撤销。
8. **写作风格**：结构化风格维度 + 样文提取 + 试写预览 + 章节级风格记录。
9. **灵感/向导**：服务端持久化会话、可编辑每步结果后再继续、参数可调、向导也走后台任务。

## Caveats / Not Found

- 未逐行阅读的部分：`wizard_stream.py` 职业体系/角色批次的完整 JSON 结构、`chapter_context_service.py` 1-1 构建器细节、`plot_expansion_service._parse_expansion_response` 全部推断规则、`PromptService.get_chapter_regeneration_prompt` 全文、`outline_generator` 的 OUTLINE_CREATE 模板全文。结论基于函数签名、关键片段与 grep 结果。
- “一对一模式下向导建章 outline_id=None 而大纲页建章设置 outline_id”的差异已在源码中核实（`wizard_stream.py:1473-1490` vs `outlines.py:_save_outlines`、`outlines.py:2776` create-single-chapter 也为 None）。
- “batch 覆盖已有内容章节”是根据 `batch_generate_chapters_in_order` 按序号筛选且无 content 过滤推断；前端默认起点为首个空章节，但若范围内后续章节已有内容会被覆盖。
- `cleanup_old_tasks` 是否被某定时任务调用：在 `background_task_service.py` 与 `main.py` 中未找到调用点。
- 未运行 MuMuAINovel，所有行为基于静态阅读。
