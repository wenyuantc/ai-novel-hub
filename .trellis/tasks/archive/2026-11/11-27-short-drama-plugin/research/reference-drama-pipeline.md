# Research: 参考项目「短剧工坊」的剧集制作流水线

- **Query**: 只读调研 `ai-drama-workshop` 的短剧/剧集制作流水线的产品功能与数据模型，产出可写需求文档的功能清单
- **Scope**: external（参考项目源码，只读）
- **Date**: 2026-11-27
- **参考项目根目录**: `/Users/wenyuantc/IdeaProjects/yty/ai-hub/ai-drama-workshop`（下文路径均相对此目录）
- **标注约定**: 【确认】= 已从代码/文档读到；【推断】= 由代码间接得出，未见直接声明

## 1. 流程概览

【确认】层级为「剧 drama → 集 episode → 分镜 storyboard」。工作台 `frontend/app/views/drama/episode.vue` 以「一集」为单位，左侧栏三大段、五个子步骤（`episode.vue:2757-2781`），底部进度条把它呈现为四段主流程「剧本 → 资产制作 → 视频制作 → 导出」（`episode.vue:2848-2853`）。

| 序 | 侧栏段 | 子步骤 key | 面板条件 | 作用 |
|---|---|---|---|---|
| 0 | （工作台之外） | 首页建剧、详情页建集 | `pages/index.vue`、`views/drama/detail.vue` | 建剧（标题/风格/画幅）、加集（标题/分辨率） |
| 1 | 剧本 | `script:raw` 原始内容 | `panel==='script' && scriptStep===0`（`episode.vue:166-189`） | 粘贴小说原文/大纲 |
| 2 | 剧本 | `script:rewrite` AI 改写 | `scriptStep===1`（`episode.vue:191-238`） | 改写为格式化剧本，或跳过改写 |
| 3 | 制作 | `prod:assets` 资产 | `panel==='production' && prodTab==='assets'`（`episode.vue:256-555`） | 提取角色/场景/道具，生成定妆照/场景图/道具图 |
| 4 | 制作 | `prod:videos` 视频制作 | `prodTab==='videos'`（`episode.vue:557-920`） | 分镜拆解 + 视频提示词 + 逐镜生视频（分镜拆分与视频生成已合并为一页，`episode.vue:557` 注释） |
| 5 | 导出 | `export:merge` 拼接导出 | `panel` 为其他值（`episode.vue:926-1078`） | 勾选镜头拼接成片、下载、标记完成 |

【确认】前置条件只有一条：没有格式化剧本（`script_content` 为空）时制作面板整体被拦截并引导回剧本步骤（`episode.vue:2712-2728`）；没有分镜时导出面板显示空态（`episode.vue:928-935`）。其余步骤可自由跳转。

【确认】顶栏常驻：文本/图片/视频三个模型下拉、分辨率下拉、刷新、任务抽屉按钮（`episode.vue:23-69`）。模型选择存 localStorage，对本页全部 Agent 与生成调用生效（`episode.vue:1747-1755`）。

【确认】流水线不经过「分镜首帧图」：`episode.vue` 中 `first_frame` / `composed_image` / `last_frame` 零命中，视频生成走「多张参考图 + 文本提示词」直出（`episode.vue:3723-3737`，`referenceMode: 'reference'` 见 `backend/src/routes/tasks.ts:187`）。后端 `pipeline-status` 接口仍保留 `generate_images` 一步（`backend/src/routes/episodes.ts:291`），属于旧口径。

## 2. 逐步骤功能清单

### 2.0 建剧与建集（工作台之外）

- 【确认】建剧表单只有三项：标题、风格预设 `style`、画幅 `aspect_ratio`（16:9 / 9:16 / 1:1 / adaptive），见 `frontend/app/pages/index.vue:362-372`、`444-453`。后端 `POST /dramas` 还接受 `description/genre/tags/metadata`（`backend/src/routes/dramas.ts:54-75`），但建剧界面未填。
- 【确认】建剧时不预建集（`dramas.ts:73` 注释）。详情页「添加下一集」对话框只填标题与分辨率（`views/drama/detail.vue:709-733`）；后端自动取下一个集号，并把当前启用的图片/视频配置锁定到该集（`backend/src/routes/episodes.ts:15-54`）。
- 【确认】画幅在建剧时固定，全剧视频统一使用（`episode.vue:2425-2426`）；分辨率按集保存，可在顶栏修改（`episode.vue:2412-2418`，`episodes.ts:61-69`，取值 480p/720p/1080p）。
- 【确认】详情页有两个主标签：剧集列表、素材库（`detail.vue:36-48`）。素材库汇总全剧角色/场景/道具，可编辑、上传图片、生成最终提示词（`detail.vue:814-1023`）。

### 2.1 剧本 · 原始内容（`script:raw`）

- 输入：一个撑满面板的 `<textarea>`，占位文案「粘贴小说原文、故事大纲或分镜描述...」（`episode.vue:184-188`，`frontend/app/locales/zh.json:650`）。
- 输出：`episodes.content`（`episode.vue:3063`，`PUT /episodes/:id`）。
- 手动：全文可编辑，手动点保存；显示去空白后的字数（`episode.vue:177`、`1621`）。
- AI：无。

### 2.2 剧本 · AI 改写（`script:rewrite`）

- 输入：`episodes.content`。
- 输出：`episodes.script_content`（格式化剧本）。
- AI：「开始改写 / 重新改写」调用 `script_rewriter`，固定消息「请读取剧本并改写为格式化剧本，然后保存」（`episode.vue:3067`）。
- 手动：改写结果在 `<textarea>` 中可直接编辑（`episode.vue:237`）；「跳过改写」把原文原样写入 `script_content` 并跳到资产步骤（`episode.vue:3068-3079`）。

### 2.3 制作 · 资产（`prod:assets`）

- 输入：本集格式化剧本。
- 输出：角色（含多套造型）、场景、道具三类资产及其图片，均关联到本集。
- AI 提取：角色/场景/道具三个独立的「提取/重新提取」按钮，空态有「一键全部提取」（`episode.vue:261-271`、`297`）；后端为异步任务，前端轮询（`episode.vue:3091-3127`，`backend/src/routes/episodes.ts:132-147`）。
- AI 生图：批量生成角色图/场景图/道具图（`episode.vue:273-284`），单卡「生成/重新生成」（`episode.vue:353-356`）；生图前若缺「最终提示词」会先由 `prompt_generator` 生成（`backend/src/services/look-generation.ts:11-33`）。
- 手动：新增角色/场景/道具（`episode.vue:1950-1977`）、删除（`1984-2010`）、详情弹窗编辑（角色：样貌+妆造；场景：描述+光影；道具：物品外貌，`episode.vue:1267-1316`）、手动改最终提示词（`2017-2021`）、上传本地图片替代生图（`3665-3686`，仅 jpg/png/webp）。
- 造型：每个角色卡片下有造型条，可切换、新增造型、把「其他造型」加入本集、设为默认、删除（`episode.vue:377-408`、`1253-1254`、`1864-1895`）。
- 素材库绑定（范围外，一句话）：角色造型/场景/道具可改用 Seedance 素材库图片，仅 Go 后端支持（`episode.vue:2190-2233`）。

### 2.4 制作 · 视频制作（`prod:videos`）

三栏布局，两条分隔条可拖拽调宽并持久化（`episode.vue:600`、`905-918`、`1699-1737`）：左栏分镜列表，中栏分镜检查器，右栏播放器与生成参数。

- 输入：格式化剧本 + 本集角色/场景/道具。
- 输出：分镜行（`storyboards`）、每镜的 `video_prompt`、每镜的视频（`storyboards.video_url` + `sys_task` 记录）。
- AI 拆镜：「开始拆分 / 重新拆分」调用 `storyboard_breaker`，消息里带上本次视频模型名与本集角色/场景/道具的「名称(ID)」清单（`episode.vue:3207-3229`）。重新拆分会清空该集旧分镜（`backend/src/agents/tools/storyboard-tools.ts:280-291`）。拆完后自动为缺提示词的分镜补跑批量视频提示词（`episode.vue:3232-3236`）。
- AI 视频提示词：单镜「AI 生成/重新生成」（`episode.vue:748-756`、`3239-3266`）；批量（全部缺失的，或选择模式下所选的）为后端异步任务（`episode.vue:3160-3181`，`backend/src/services/video-prompts.ts:27-105`）。
- AI 生视频：单镜生成（`episode.vue:3723-3759`）、批量生成（带确认弹窗，显示数量与总时长，`episode.vue:2506-2543`）、重试失败（`2519`）。同一分镜可多次生成，历史全部保留。
- 手动可编辑：画面描述 `description`、氛围 `atmosphere`（失焦即保存，`episode.vue:691`、`699`）；参考素材绑定（场景单选、角色造型/道具多选，`episode.vue:3578-3590`）；视频提示词（支持 `@` 提及，`episode.vue:758-765`）；时长 2-30 秒（`episode.vue:879-886`、`3635-3645`）。
- 只读展示：分镜技巧标签 `techniques`（Go 独有，`episode.vue:693-696`）、已绑定参考图缩略图（`851-869`）、本次生效配置小结（模型/分辨率/时长，`892-894`）。
- 列表功能：按待生成/已完成/失败筛选（`episode.vue:608-610`）；选择模式下全选/仅选未生成/清空（`613-617`）。
- 播放器：播放当前主视频或历史版本、下载、「设为主视频」、删除历史记录（`episode.vue:781-796`、`826-846`、`3456-3478`）。
- 【确认】界面不提供单条分镜的新增与删除：`storyboardAPI.create/del` 在 `episode.vue` 中零调用，只有 `update`（`episode.vue:1671`、`2908`、`2949`、`3464`）。分镜条数只能靠重新拆分改变。

### 2.5 导出 · 拼接导出（`export:merge`）

- 输入：已有视频的分镜。
- 输出：成片记录（`video_merges`），成片文件可下载。
- 上半区「成片列表」：历次拼接记录卡片，可预览、下载；「标记完成」把 `episodes.status` 置为 `completed`（`episode.vue:939-1000`、`1805-1815`）。
- 下半区「镜头素材」：分镜网格，勾选要拼接的镜头，「全选可用」「拼接所选」（`episode.vue:1003-1075`）；镜头有多版视频时可点版本角标切换（`1046-1056`）。
- 拼接：`POST /merge/episodes/:id/merge`，可传 `storyboard_ids`，按镜号顺序拼，未生成的跳过（`backend/src/routes/merge.ts:12-34`，`backend/src/services/ffmpeg-merge.ts:26-41`）；前端每 3 秒轮询状态（`episode.vue:3801-3826`）。
- AI：无（FFmpeg 本地拼接）。
- 【确认】未见字幕、配音、配乐、转场的界面入口；`storyboards` 表有 `bgm_prompt/sound_effect/subtitle_url` 列，但 `episode.vue` 不读写它们（检索零命中）。生视频请求固定带 `generate_audio: true`（`episode.vue:3733`），声音由视频模型一并生成。

### 2.6 任务抽屉（全程可用）

- 顶栏「任务」按钮打开右侧抽屉，按集聚合图片/视频/拼接三类任务，按时间倒序，有进行中任务时每 4 秒轮询（`episode.vue:1080-1160`、`2564-2710`；接口 `GET /episodes/:id/generation-tasks`，`backend/src/routes/episodes.ts:223-262`）。

## 3. 数据模型

DDL 文件：`backend/src/db/sqlite-schema.ts`（下表行号均指此文件）；Drizzle 定义在 `backend/src/db/schema.ts:8-340`；Go 版同构 DDL 在 `backend-go/internal/db/schema/ddl.sql:24-354`。全部表无外键约束，关联靠整数 id 列；主体表用 `deleted_at` 软删除。

### 3.1 核心实体表（7 张）

| 表 | 行号 | 用途 | 关键字段 |
|---|---|---|---|
| `dramas` | 29-45 | 剧（项目） | `title`、`style`（风格预设 key，默认 `3d`）、`aspect_ratio`（默认 16:9）、`status`、`description/genre/tags/metadata`、`total_episodes/total_duration` |
| `episodes` | 47-65 | 集；**剧本直接存在本表，没有独立剧本表** | `drama_id`、`episode_number`、`title`、`content`（原文）、`script_content`（格式化剧本）、`duration`（分钟）、`status`、`video_url`、`image_config_id/video_config_id`（建集时锁定）、`resolution`（默认 720p） |
| `characters` | 67-85 | 角色（剧级，跨集复用） | `drama_id`、`name`、`role`、`description`、`appearance`（样貌，跨造型不变）、`personality`、`sort_order`；`styling/final_prompt/image_url/local_path` 只是默认造型的镜像 |
| `character_looks` | 171-184 | 角色造型；妆造/最终提示词/定妆照的事实来源 | `character_id`、`name`、`styling`、`final_prompt`、`image_url/local_path`、`is_default`、`sort_order` |
| `scenes` | 87-103 | 场景（剧级） | `drama_id`、`episode_id`（旧数据直挂）、`location`、`time`、`prompt`（场景描述）、`lighting`、`final_prompt`、`image_url`、`status` |
| `props` | 301-315 | 道具（剧级） | `drama_id`、`name`、`type`、`description`（物品外貌）、`prompt`、`final_prompt`、`image_url` |
| `storyboards` | 105-135 | 分镜（集级，一行 = 一个视频生成任务） | 见第 5 节 |

### 3.2 关联表（7 张）

| 表 | 行号 | 关系 | 主键/约束 |
|---|---|---|---|
| `episode_characters` | 137-142 | 集 ↔ 角色（本集出场） | 自增 id，无唯一约束 |
| `episode_scenes` | 146-151 | 集 ↔ 场景 | 自增 id |
| `episode_props` | 155-160 | 集 ↔ 道具 | 自增 id |
| `episode_character_looks` | 187-193 | 集 ↔ 造型（本集用到哪些造型） | 唯一索引 `(episode_id, look_id)`（193） |
| `storyboard_characters` | 164-168 | 分镜 ↔ 角色 | 复合主键 `(storyboard_id, character_id)` |
| `storyboard_character_looks` | 195-200 | 分镜 ↔ 造型（含冗余 `character_id`） | 复合主键 `(storyboard_id, look_id)`；同一角色可多行 = 镜头内换装 |
| `storyboard_props` | 203-207 | 分镜 ↔ 道具 | 复合主键 `(storyboard_id, prop_id)` |

分镜 ↔ 场景不用关联表，直接是 `storyboards.scene_id`（单选）。

### 3.3 任务与导出表（2 张）

| 表 | 行号 | 用途 | 关键字段 |
|---|---|---|---|
| `sys_task` | 257-278 | 生图/生视频任务（通用）；**同时充当分镜视频版本历史** | `type`（image/video）、`storyboard_id/drama_id/scene_id/character_id/prop_id/look_id`（归属键，无 `episode_id`）、`provider/model/prompt/params`、`task_id`（厂商任务号）、`result_url/local_path`、`status`、`error_msg` |
| `video_merges` | 283-299 | 成片（拼接记录），一集可多条 | `episode_id/drama_id`、`status`、`scenes`、`merged_url`、`duration`、`error_msg` |

### 3.4 共用支撑表（短剧流水线会读，但不专属短剧）

| 表 | 行号 | 与短剧的关系 |
|---|---|---|
| `style_presets` | 242-255 | `dramas.style` 存其 `value`；`prompt` 作为生图提示词前缀，`video_prompt/video_negative_prompt` 注入视频提示词。种子 9 种风格（369-433）：3D 漫剧、日漫赛璐璐、吉卜力手绘、水彩绘本、美式漫画、真人写实、国风 2.5D、韩系网漫、黑白漫画 |
| `ai_service_configs` | 210-226 | 文本/图片/视频服务配置；集上锁定其 id |
| `ai_service_providers` | 228-240 | 厂商预设 |
| `app_settings` | 346-350 | key-value，全局「AI 内容语言」等 |
| `assets` | 317-343 | 通用素材表；TS 后端源码中对 `schema.assets` 零引用【确认：检索无结果】 |

范围外：`tool_runs`（10-26，工具中心）。

### 3.5 Go 后端独有表（不在 TS DDL 中）

| 表 | 位置 | 用途 |
|---|---|---|
| `storyboard_techniques` | `backend-go/internal/db/techniqueschema.go:12-18` | 分镜按【镜头N】子镜头存所选视频技巧；列 `storyboard_id, sub_index, ord, technique_id`，三列复合主键 |
| `asset_hub_bindings` | `backend-go/internal/db/assetbindschema.go:12-24` | 资源（造型/场景/道具）与 Seedance 素材库素材的绑定（范围外） |

### 3.6 状态枚举

| 字段 | 取值 | 证据 |
|---|---|---|
| `dramas.status`、`episodes.status` | `draft` / `active` / `completed`；自动推进只升不降：集有剧本或分镜 → active，有成片 → completed；剧的全部集 completed → completed | `backend/src/db/progress-status.ts:9-31` |
| `episodes.status = completed` | 也可由用户在导出页手动标记 | `episode.vue:1805-1815` |
| `sys_task.status` | `processing`（默认）/ `completed` / `failed` | `backend/src/services/generation.ts:202`、`389`、`493` |
| `video_merges.status` | `pending`（列默认）/ `processing` / `completed` / `failed` | `sqlite-schema.ts:290`，`backend/src/services/ffmpeg-merge.ts:70`、`81`、`139` |
| `scenes.status` | `pending` / `processing` / `completed` / `failed` | `sqlite-schema.ts:98`，`backend/src/routes/scenes.ts:55`、`89`、`95` |
| `storyboards.status` | 列默认 `pending`；TS 后端未见写入，界面状态由前端推导（done/pending/failed/ready） | `sqlite-schema.ts:131`，`episode.vue:2307-2312` |
| 提取任务、批量视频提示词任务 | `running` / `done` / `error`；**进程内内存态，不入库**，重启丢失 | `backend/src/services/extraction.ts:14-21`，`backend/src/services/video-prompts.ts:12-23` |

### 3.7 需求清单里点名、但参考项目中不存在的表

- 【确认】没有「剧本」表：剧本是 `episodes.content` 与 `episodes.script_content` 两列。
- 【确认】没有「分镜版本」表：版本由 `sys_task` 行推导（见 5.3）。
- 【确认】没有「分镜参考」表：参考图由分镜的场景/造型/道具绑定实时计算（见 5.4）。
- 【确认】没有「导出」专表之外的导出配置：成片即 `video_merges`。

## 4. 四个 Agent 与提示词结构

### 4.1 共同机制

- 【确认】四个 Agent 在启动时静态注册，`instructions` 与 `model` 按请求动态解析（`backend/src/agents/index.ts:330-343`）。
- 【确认】系统提示 = 提示词文件（`backend/workspace/prompts/<type>.md`，缺失时回退代码内默认）+ 该 Agent 的技能全文（`backend/workspace/skills/`）+ 目标语言指令块，三段拼接（`agents/index.ts:304-316`）。提示词与技能各有 zh/en/ja/ko 四份语言变体。
- 【确认】提示词与技能可在设置页在线编辑、重置（`backend/src/routes/prompts.ts:19-87`，`backend/src/routes/skills.ts:17-91`）。
- 【确认】`episodeId/dramaId` 不由模型传参，而是经请求上下文注入，工具内读取（`backend/src/agents/context.ts:17-37`）。因此所有工具都隐式绑定「当前集」。
- 【确认】**不是流式对话，是一次性任务**。入口 `POST /agent/:type/chat`，文件头注释即「非流式版本」（`backend/src/routes/agent.ts:1-3`、`33`）；请求体 `{message, drama_id, episode_id, model?, config_id?}`，无会话历史，每次只发一条 user 消息，`maxSteps: 20`（`agent.ts:70`）；响应一次性返回 `{type:'done', text, toolCalls, toolResults}`（`agent.ts:94-99`）。服务端内部用流式调用模型只是为了防网关 60 秒超时（`backend/src/agents/run.ts:3-9`）。
- 【确认】前端不展示 Agent 的文本回复，只在完成后 toast 并刷新数据（`frontend/app/composables/useAgent.ts:10-30`）；同一时刻只允许跑一个 chat 型 Agent（`useAgent.ts:11`）。
- 【确认】Go 版同样是一次性 JSON 响应，且客户端断开后仍跑完（`backend-go/internal/api/agent.go:35-72`）。

### 4.2 触发方式对照

| Agent | 触发入口 | 同步/异步 | 证据 |
|---|---|---|---|
| `script_rewriter` | 前端直接调 `/agent/script_rewriter/chat` | 同步等待（HTTP 长请求） | `episode.vue:3067` |
| `extractor` | `POST /episodes/:id/extract`，`target` = characters / scenes / props，各自一条固定消息 | 后端异步，三类可并行，前端轮询 `extract-status` | `backend/src/services/extraction.ts:25-83` |
| `storyboard_breaker` | 前端直接调 `/agent/storyboard_breaker/chat` | 同步等待 | `episode.vue:3207-3229` |
| `prompt_generator`（视频提示词，单镜） | 前端直接调 `/agent/prompt_generator/chat` | 同步等待，不同分镜可并行 | `episode.vue:3239-3266` |
| `prompt_generator`（视频提示词，批量） | `POST /episodes/:id/generate-video-prompts` | 后端异步逐镜串行，`maxSteps: 8`，前端轮询 | `backend/src/services/video-prompts.ts:27-105` |
| `prompt_generator`（图片最终提示词） | 各资产的 `generate-prompt` / `generate-image` 接口内部调用 | 随接口同步，`maxSteps: 12` | `backend/src/services/final-prompt.ts:24-84` |

### 4.3 `script_rewriter`（小说 → 剧本）

- 输入：`episodes.content`（为空时回退 `script_content`）。
- 工具（`backend/src/agents/tools/script-tools.ts`）：
  - `read_episode_script`（12-26）：读 `episodes`，返回 `{content, word_count, episode_id}`。
  - `rewrite_to_screenplay`（28-59）：读 `episodes`，返回原文 + 格式说明（可带额外改写要求）。
  - `save_script`（61-77）：写 `episodes.script_content`，并推进集状态。
- 产出结构（`backend/workspace/prompts/script_rewriter.md:13-17`，`backend/workspace/skills/script-rewriter/SKILL.md:16-39`）：
  - 场景头 `## S编号 | 内景/外景 · 地点 | 时间段`，编号连续递增。
  - 动作描写为自然段，不含镜头语言（景别/角度/运镜留给分镜步骤）。
  - 对白 `角色名：（状态/表情）台词内容`。
  - 每个场景 30-60 秒内容；篇幅比原文增约 20-30%，不扩写。
- 【确认】产出是纯文本（Markdown 风格），不是结构化 JSON；下游按文本读取。

### 4.4 `extractor`（角色/场景/道具提取）

- 输入：本集格式化剧本 + 项目已有角色/场景/道具（用于去重）。
- 工具（`backend/src/agents/tools/extract-tools.ts`）：

| 工具 | 行号 | 读/写的表 |
|---|---|---|
| `read_script_for_extraction` | 109-126 | 读 `episodes` |
| `read_existing_characters` | 128-171 | 读 `characters`、`episode_characters`、`character_looks` |
| `read_existing_scenes` | 173-209 | 读 `scenes`、`episode_scenes` |
| `read_existing_props` | 357-392 | 读 `props`、`episode_props` |
| `save_dedup_characters` | 211-287 | 写 `characters`、`character_looks`、`episode_characters`、`episode_character_looks` |
| `save_dedup_scenes` | 289-355 | 写 `scenes`、`episode_scenes` |
| `save_dedup_props` | 394 起 | 写 `props`、`episode_props` |

- 产出字段（工具入参即产出结构）：
  - 角色：`name`、`role`、`description`、`appearance`（样貌）、`looks[]`（每项 `{name, styling}`），见 `extract-tools.ts:214-224`。
  - 场景：`location`、`time`、`prompt`（场景描述）、`lighting`（`extract-tools.ts:292-298`）。
  - 道具：`name`、`type`、`description`（只写物理外观，`extract-tools.ts:397-401`）。
- 去重规则（`backend/workspace/prompts/extractor.md:27-30`）：角色/道具按名字匹配，带括号的定位/别名按括号前主体比较；场景按「地点 + 时间段」匹配；造型按归一化造型名匹配，已有定妆照的造型不被覆盖。
- 道具筛选（`extractor.md:19-23`）：必须同时满足「直接推动剧情」与「值得单独出图」，一集通常 0-3 个，可为空。
- 造型判定（`extractor.md:39-44`）：明确换装/礼服/制服/伪装或时间跳跃才算新造型；脱外套等小变化不算；命名 2-6 字、不含角色名。
- 【确认】提取范围限定为「当前集」，但资产是剧级的，跨集复用并合并更新（`extract-tools.ts:239-275`）。

### 4.5 `storyboard_breaker`（分镜拆解）

- 输入：本集剧本 + 本集角色（含造型）/场景/道具 + 已有分镜。
- 工具（`backend/src/agents/tools/storyboard-tools.ts`）：
  - `read_storyboard_context`（109-232）：读 `episodes`、`characters`、`character_looks`、`scenes`、`props`、`storyboards` 及各关联表；返回 `{episode, script, characters[], scenes[], props[], existing_storyboards[]}`。本集已有关联时只返回本集关联的资产（147-183）。
  - `save_storyboards`（256-366）：写 `storyboards`、`storyboard_characters`、`storyboard_character_looks`、`storyboard_props`，必要时补写 `episode_*` 关联，并回写 `episodes.duration`。首批带 `replace_existing: true` 清空旧分镜；按 `shot_number` 幂等 upsert；每批最多 8 条。
  - `update_storyboard`（368-476）：按 id 局部更新分镜及其绑定。
- 产出结构：见第 5 节分镜字段。提示词层面的硬规则（`backend/workspace/prompts/storyboard_breaker.md`）：
  - 一个分镜 = 一个「分镜段落」= 一个视频生成任务；8-15 秒，内含 2-4 个子镜头，可切镜但不跨场景（第 8 行）。
  - 总量锚定：目标总时长 = 剧本字数 ÷ 500 字/分钟，段落数 ≈ 总时长 ÷ 12 秒，±20%（第 34 行）。
  - 台词下限：段落时长 ≥ 台词与旁白字数 ÷ 4.5 字/秒 + 2 秒（第 36 行）。
  - 只允许输出工具调用，不输出规划文字（第 17-20 行）。
- 【确认】Go 版提示词多一步 `read_video_techniques` 与一个 `techniques` 字段（`storyboard_breaker.md:12`、`31`），对应 Go 独有表 `storyboard_techniques`；TS 版代码内默认提示词没有这一步（`backend/src/agents/index.ts:91-95`）。

### 4.6 `prompt_generator`（图片/视频提示词）

- 工具 = 图片提示词 6 个 + 复用分镜的 2 个（`backend/src/agents/index.ts:295-299`）：
  - `read_characters` / `read_scenes` / `read_props`（`backend/src/agents/tools/image-prompt-tools.ts:22-50`、`87-108`、`141-161`）：读对应资产表（角色含 `character_looks`）。
  - `save_character_final_prompt`（52-83，必须带 `look_id`，写 `character_looks.final_prompt`）、`save_scene_final_prompt`（110-137，写 `scenes.final_prompt`）、`save_prop_final_prompt`（163-190，写 `props.final_prompt`）。保存时工具自动把剧的风格预设词拼在最前（71、125、178 行）。
  - `read_storyboard_context`、`update_storyboard`：读分镜上下文、只写 `storyboards.video_prompt`。
- 图片最终提示词的三种固定版式（`backend/src/services/final-prompt.ts:3-6`，技能在 `backend/workspace/skills/prompt-generator/`）：角色造型 = 正脸特写 + 三视图；场景 = 广角固定视角、前/中/后景分层、**无人物空镜**；道具 = 白底单品静物。
- 视频提示词版式（`backend/workspace/prompts/prompt_generator.md:26-31`）：按 3 秒一段、每段一行；每个【镜头N】映射 1-2 段；用 `@角色名` / `@场景名` / `@角色名·造型名` 引用参考图；只写正向描述；末尾固定一行「整体：…」。
- 【确认】`seedance-prompt-writing`、`h3-prompt-writing` 两份技能不属于这四个 Agent：它们由 `backend/src/services/video-prompt-rewrite.ts:3-4`、`89`、`117` 加载，调用方是工具中心（`backend/src/services/tools.ts:9`），短剧流水线不经过。

## 5. 分镜细节

### 5.1 `storyboards` 表字段（`backend/src/db/sqlite-schema.ts:105-135`）

| 分组 | 字段 | 说明 | 界面是否使用 |
|---|---|---|---|
| 归属与顺序 | `episode_id`、`storyboard_number` | 集内镜号，Agent 传 `shot_number` | 是（列表序号） |
| 场景 | `scene_id`、`location`、`time` | `scene_id` 单选绑定场景 | `scene_id` 是；`location/time` 否 |
| 镜头语言 | `shot_type`（景别）、`angle`（角度）、`movement`（运镜） | Agent 可写 | 否（`episode.vue` 检索零命中） |
| 文本 | `title`、`description`（画面描述）、`result`（画面结果）、`atmosphere`（氛围） | `description` 按【镜头1】【镜头2】…分子镜头，**台词与旁白写在 description 内**，格式「角色名说：「台词」」「旁白：内容」 | `description`、`atmosphere` 可编辑；`title`、`result` 否 |
| 提示词 | `image_prompt`、`video_prompt`、`bgm_prompt`、`sound_effect` | `video_prompt` 为视频生成直接使用 | 仅 `video_prompt` |
| 时长 | `duration`（秒，默认 0；保存时缺省写 10） | Agent 规则 8-15 秒；界面可改 2-30 秒 | 是 |
| 图像 | `composed_image`、`first_frame_image`、`last_frame_image`、`reference_images` | 首帧/尾帧/合成图列 | 否（零命中） |
| 视频 | `video_url`（当前主视频）、`composed_video_url`（历史兼容）、`subtitle_url` | 拼接读 `video_url`，回退 `composed_video_url` | `video_url` 是 |
| 状态 | `status`、时间戳、`deleted_at` | | 否（前端自行推导） |

- 【确认】**没有独立的台词字段**，台词内嵌在 `description` 文本里（`backend/workspace/prompts/storyboard_breaker.md:28`）。
- 【确认】分镜引用的角色/造型/道具不在本表，而在三张关联表；读取接口把它们展开为 `character_ids`、`look_ids`、`prop_ids`、`characters`、`props` 一并返回（`backend/src/routes/episodes.ts:206-217`）。
- 【确认】Agent 入参字段全集见 `backend/src/agents/tools/storyboard-tools.ts:234-254`；HTTP 可更新字段白名单见 `backend/src/routes/storyboards.ts:152-160`。
- 【确认】绑定校验：经 HTTP 更新时，场景/角色/道具必须已关联到当前集（`storyboards.ts:55-79`）；经 Agent 保存时，只要属于本剧就自动补关联到当前集（`storyboard-tools.ts:65-98`）。
- 【确认】Go 版读取分镜时额外返回 `techniques`（按子镜头分组的技巧 id 数组），界面只读展示（`episode.vue:693-696`，`backend-go/internal/service/sbtech/sbtech.go:1-30`）。每组须恰好 1 个运镜类 + 最多 2 个其他技巧（`backend-go/internal/service/technique/technique.go:20-26`）。

### 5.2 分镜在界面上的状态

前端推导四态（`episode.vue:2307-2312`）：有视频 = `done`；在本页待生成列表中 = `pending`；有失败消息 = `failed`；其余 = `ready`。待生成与失败信息保存在页面内存中（`episode.vue:1782-1783`）【推断：刷新页面后依赖任务记录重新同步，未逐行核对恢复逻辑】。

### 5.3 分镜版本机制（`frontend/app/utils/shot-versions.ts`，49 行）

- 【确认】没有版本表。版本 = 该分镜下全部 `type='video'` 且 `status='completed'` 且有文件路径的 `sys_task` 行（`shot-versions.ts:26-27`）。
- 按 `created_at` 降序（同刻按 id 降序）排列，同一文件被多条任务引用时只留最新一条（`shot-versions.ts:21-35`）。
- 版本号按生成先后从 1 编号，最新的号最大（`shot-versions.ts:36-42`）。
- 「当前版本」= 路径等于 `storyboards.video_url` 的那一条；主视频不在历史里时为空（`shot-versions.ts:46-49`）。
- 切换版本 = 把 `storyboards.video_url` 改成所选版本的路径（`episode.vue:1666-1680`）。拼接只读主视频，所以「选哪一版拼接」等价于「设哪一版为主视频」（`episode.vue:1653` 注释）。
- 两个入口：导出页镜头卡片的版本角标打开 `ShotVersionPicker` 弹窗（`episode.vue:1046-1056`、`1436`，组件 207 行，事件 `select`/`close`）；视频制作页右栏的历史条「设为主视频」（`episode.vue:826-846`、`3460-3469`）。
- 删除某个历史版本 = 删除对应 `sys_task` 行（`episode.vue:3471-3478`，`backend/src/routes/tasks.ts:240-244`）；当前主视频那条不显示删除按钮（`episode.vue:843-844`）。
- 【确认】版本只针对视频。分镜的文本字段（描述、提示词）没有版本历史，重新拆分直接覆盖。

### 5.4 分镜参考机制（`frontend/app/utils/shot-references.ts`，152 行）

- 【确认】没有参考表，也不读 `storyboards.reference_images` 列。参考图在点「生成视频」时由前端按绑定实时计算，随请求以 `reference_image_urls` 提交（`episode.vue:3488-3497`、`3723-3737`）。
- 参考图来源与顺序：场景图 → 角色造型定妆照（每套绑定造型一张）→ 道具图；URL 去重；超过上限的丢弃（`shot-references.ts:56-83`）。上限默认 9 张，Wan 3.0 为 10 张（`episode.vue:2488-2489`）。
- 造型解析：分镜显式绑定了造型就用绑定的（默认造型排前）；只绑了角色没绑造型则用该角色默认造型（`shot-references.ts:28-44`）。
- 造型没有定妆照时直接跳过，不用默认造型顶替，并记入 `missingLooks`（`shot-references.ts:78`）；界面提示「造型未生成定妆照」并给出跳转（`episode.vue:730`、`735`）。
- 名称注册：每套造型注册「角色名·造型名」，同时把裸「角色名」指向该角色本镜第一套有图的造型；场景按 `location` 注册，道具按 `name` 注册（`shot-references.ts:74-81`）。
- `@` 替换：生成前把提示词里的 `@名称` 替换成 `@图片N名称`（N 为参考图序号，从 1 起），按最长注册名优先匹配；分隔符 `·`、`・`、`•` 视为等价；重复执行结果不变（`shot-references.ts:90-115`）。
- 绑定交互：右侧面板列出本集全部可绑定素材，场景单选、角色造型与道具多选（`episode.vue:3505-3590`）；点造型的切换规则见 `shot-references.ts:135-152`（取消某角色最后一套造型即把该角色移出分镜）。
- `@` 提及输入：`MentionTextarea` 组件（467 行）用镜像层给 `@引用` 着色，候选只含当前分镜已绑定的角色、场景、道具（`frontend/app/components/MentionTextarea.vue:1-12`，`episode.vue:3594-3621`）。
- 后端在提交视频任务时再把剧的视频风格词拼到提示词最前（`backend/src/routes/tasks.ts:161-167`）。

### 5.5 角色造型（looks）要点（设计文档 `docs/superpowers/specs/2026-10-12-character-looks-design.md`）

- 目标：一个角色多套造型、造型跨集复用、同一分镜内可换装（设计文档 14-18 行）。
- 关键决策（20-28 行）：造型只换妆造（发型/服装/妆面/配饰），样貌 `appearance` 属于角色、各造型共用；造型来源为 AI 自动识别 + 手动调整；已有定妆照的造型不被提取覆盖；非默认造型生图时以默认造型定妆照作参考图以保持同脸。
- 非目标（30-32 行）：不支持造型覆盖样貌（年龄跨度、受伤、变身）。
- 镜像规则：造型表是事实来源，`characters` 上的 `styling/final_prompt/image_url/local_path` 只是默认造型的镜像（`backend/src/services/character-looks.ts:2-3`、`68`）；每次启动做一次对账（`backend/src/db/migrations.ts:131-158`）。
- 约束：每个角色至少一套造型；默认造型不可删；同角色下造型名不可重复（`character-looks.ts:82-84`、`157-161`）。
- 接口：`backend/src/routes/character-looks.ts:45-99`（改、删、设默认、加入本集、生图、生成提示词），新增在 `backend/src/routes/characters.ts:77-93`。

## 6. 「小说 → 剧本」入口

- 【确认】**唯一入口是粘贴**。原始内容步骤只有一个 `<textarea>`，占位文案「粘贴小说原文、故事大纲或分镜描述...」（`episode.vue:184-188`，`frontend/app/locales/zh.json:650`）。
- 【确认】**没有文本文件上传**。短剧两个视图里的文件选择只接受图片（`episode.vue:3666`，`detail.vue:863`、`978`，均为 `image/jpeg,image/png,image/webp`）；后端上传路由只有图片/视频/音频三类（`backend/src/routes/upload.ts:8`、`64`、`67`）。
- 【确认】**没有章节概念，也没有自动分集**。在 `backend/src` 与 `backend-go/internal` 中检索 `chapter` / `章节` / `分集` 均零命中。集由用户在详情页逐集手动添加（`detail.vue:709-733`），每集各自粘贴一段原文；一段长文不会被拆成多集。
- 【确认】粒度是「一集一段原文」：`episodes.content` 存原文，`episodes.script_content` 存改写结果，一对一（`backend/src/db/sqlite-schema.ts:52-53`）。
- 【确认】**代码中没有长度限制或截断**：保存接口不校验长度（`backend/src/routes/episodes.ts:57-83`）；`read_episode_script` 把全文原样交给模型，只附带 `word_count`（`backend/src/agents/tools/script-tools.ts:22-24`）。界面只显示字数，不设上限（`episode.vue:177`）。
- 【确认】实际上限来自模型侧：非官方 OpenAI 端点的输出上限被设为 16384 token（可用环境变量 `AI_MAX_TOKENS` 覆盖），注释说明这是为了避免长输出被截断导致「正常结束但什么都没保存」（`backend/src/agents/index.ts:202-215`）。
- 【确认】改写不是必经步骤：可「跳过改写」把原文直接当剧本用（`episode.vue:3068-3079`）；下游读取剧本时也都带回退，`script_content` 为空就读 `content`（`backend/src/agents/tools/storyboard-tools.ts:120`）。
- 【确认】原文的篇幅会影响后续分镜数量：拆镜规则按「剧本字数 ÷ 500 字/分钟」估目标总时长（`backend/workspace/prompts/storyboard_breaker.md:34`）；改写规范要求每个场景 30-60 秒、篇幅只增 20-30%（`backend/workspace/skills/script-rewriter/SKILL.md:13`、`39`）。
- 【确认】后端有一个批量保存集的接口 `PUT /dramas/:id/episodes`（`backend/src/routes/dramas.ts:163-185`），但前端 `dramaAPI` 未封装它（`frontend/app/composables/useApi.ts:39-44`），界面没有批量建集入口。

## 7. 画布结论

**结论：参考项目没有画布（节点图 / 拖拽连线 / 无限画布）功能。初步判断成立。**

证据：

1. 【确认】前端依赖只有 7 个：`driver.js`、`lucide-vue-next`、`nuxt`、`vue`、`vue-i18n`、`vue-router`、`vue-sonner`（`frontend/package.json` 的 `dependencies`）。没有任何图编辑/画布库。
2. 【确认】全仓源码与文档（排除 `node_modules`、构建产物、`.wolf`、`data`）检索 `canvas|画布|vue-flow|antv|konva|fabric|reactflow|jsplumb|drawflow|litegraph|节点图|无限画布|whiteboard|tldraw|excalidraw`，命中全部与画布功能无关，逐条如下：
   - `frontend/app/composables/useTools.ts:227-229`：用 `<canvas>` 元素从视频抽取一帧（工具中心）。
   - `frontend/app/views/tools/workspace.vue:115`、`426`、`491`：CSS 类名 `.stage-canvas`，指工具中心的结果展示区。
   - `backend/src/routes/props.ts:12`：注释「道具图：白底单品静物，方形画布」，指图片尺寸。
   - `backend/src/services/adapters/openai-image.ts:50`、`openai-video.ts:54`、`docs/tools.md:134`：指输出图像/视频的画幅。
   - `backend-go/internal/service/tools/media.go:274-277`、`backend-go/internal/service/suite/finalize.go:23-24`、`suite/compose/size.go:27`：Go 图像合成时的底图变量。
   - `docs/design/2026-10-21-tools-redesign.html:106-107`、`245`：工具中心改版稿里的 CSS 类 `.canvas`，同样指结果展示区。
   - `backend/tests/openai-video.test.mjs:80`、`backend/tests/tools.test.mjs:279`、`backend-go/internal/adapters/openai_image.go:66`：指首帧视频/图片编辑的输出画幅。
   - 其余命中是子串或内嵌数据误匹配（`fabric` 命中 `fabrics`/`fabricate`；`docs/design/2026-10-21-ecom-suite-mockup.html` 命中的是 base64 图片数据）。
3. 【确认】短剧视图 `frontend/app/views/drama/*.vue` 与 `frontend/app/components/*.vue` 中检索 `draggable|dragstart|@drop|sortable` 零命中。唯一的拖拽交互是视频制作页两条分栏分隔条的宽度调节（`episode.vue:905-918`、`1715-1737`）。
4. 【确认】短剧工作台是固定的线性分步界面：左侧栏步骤 + 主面板 `v-if` 切换（`episode.vue:166`、`242`、`927`），分镜以纵向列表呈现（`episode.vue:618-680`），导出页为网格（`episode.vue:1021-1074`）。
5. 【确认】设计文档中没有画布相关的设计稿：`docs/superpowers/specs/` 共 11 份规格（造型、Go 迁移、MV、技巧目录、风格重绘、UGC、素材库绑定），`docs/design/` 下是工具中心、电商套图、风格重绘、素材库的界面稿，文件名与内容均不涉及节点图或画布。

## 8. 规模

行数均为 `wc -l` 实测（2026-11-27），Go 侧不含 `_test.go`。

### 8.1 前端（短剧相关）

| 文件 | 行数 | 说明 |
|---|---|---|
| `frontend/app/views/drama/episode.vue` | 6625 | 模板 1-1568、脚本 1570-3857（约 2290 行）、样式 3859-6625（约 2770 行） |
| `frontend/app/views/drama/detail.vue` | 1750 | 剧详情：剧集列表 + 素材库 |
| `frontend/app/pages/index.vue` | 933 | 首页，与 MV/UGC 共用 |
| `frontend/app/components/MentionTextarea.vue` | 467 | `@` 提及输入 |
| `frontend/app/components/ShotVersionPicker.vue` | 207 | 版本选择弹窗 |
| `frontend/app/utils/shot-references.ts` | 152 | 参考图与 `@` 替换纯函数 |
| `frontend/app/utils/shot-versions.ts` | 49 | 版本分组纯函数 |
| `frontend/app/composables/useApi.ts` | 204 | 全部接口封装（含非短剧） |
| `frontend/app/composables/useAgent.ts` | 33 | Agent 调用封装 |
| `frontend/app/locales/zh.json` | 1919 | 全站文案；`episode` 块自第 602 行起 |

工作台单文件 `episode.vue` 即占短剧前端的绝大部分；页面内另引用素材库相关组件 3 个与 composable 2 个（`episode.vue:1586-1590`，范围外）。

### 8.2 TS 后端（`backend/src`，已冻结）

| 范围 | 文件数 | 行数 |
|---|---|---|
| 短剧相关路由：agent、dramas、episodes、storyboards、characters、character-looks、scenes、props、tasks、merge、stylePresets、prompts、skills | 13 | 1927 |
| Agent：`agents/*.ts` 8 个 + `agents/tools/*.ts` 4 个 | 12 | 2096 |
| 短剧相关服务：character-looks、extraction、ffmpeg-merge、final-prompt、generation、look-generation、look-image、storyboard-looks、style-preset、video-prompts、video-style、task-errors | 12 | 1600 |
| 数据层 `db/*.ts` | 7 | 1319 |

其中 `services/generation.ts`（702 行）是生图/生视频任务引擎，与工具中心共用；适配器目录 `services/adapters/` 未计入（另一份调研负责）。

### 8.3 Go 后端（`backend-go/internal`，实际发布用）

| 范围 | 文件数 | 行数 |
|---|---|---|
| 短剧相关 API：agent、dramas、episodes、episodes_agent、episodes_read、storyboards、characters、character_looks、scenes、props、tasks、tasks_create、merge、final_prompt_routes、style_presets、prompts、skills、techniques、asset_image_routes | 19 | 3283 |
| 短剧相关服务目录：agentjobs（287）、finalprompt（225）、generation（1634）、looks（753）、merge（141）、sbtech（113）、technique（203） | 21 | 3356 |
| 自研 Agent 运行时 `internal/agent/`（替代 Mastra；含指令加载、工具定义、循环、传输） | 36 | 6180 |

【推断】`internal/agent/` 的 6180 行是全部 Agent 共用的运行时（MV/UGC 的 Agent 也在其上），不能全算作短剧代码；未逐文件划分。

### 8.4 提示词与技能（`backend/workspace`）

- 四个 Agent 的中文提示词共 165 行（`script_rewriter.md` 19、`extractor.md` 44、`storyboard_breaker.md` 65、`prompt_generator.md` 37），各有 en/ja/ko 三份变体。
- 技能中文版：`script-rewriter` 56、`extractor` 65、`storyboard-breaker` 83、`prompt-generator` 下四份（character 55、scene 61、prop 43、video 91），各有三份语言变体。

## 9. Go 后端相对 TS 版在短剧流水线上的增量

| 增量 | 位置 | 说明 |
|---|---|---|
| 分镜视频技巧 | 表 `storyboard_techniques`（`backend-go/internal/db/techniqueschema.go:12-18`）；服务 `internal/service/sbtech/`、`internal/service/technique/`；接口 `internal/api/techniques.go` | 拆镜 Agent 多一步读取技巧目录并为每个子镜头选技巧；提示词 Agent 多一步 `read_storyboard_techniques`（`backend/workspace/prompts/prompt_generator.md:28`）；界面只读展示 |
| 素材库绑定 | 表 `asset_hub_bindings`（`backend-go/internal/db/assetbindschema.go:12-24`） | 范围外；与短剧的耦合点是参考图地址可被绑定素材替换（`episode.vue:3494-3495`） |
| Agent 运行时 | `backend-go/internal/agent/` | 自研实现，对外接口与 TS 版一致（`backend-go/internal/api/agent.go:15-16`） |

【确认】`backend/workspace/prompts/` 下的提示词文件是两个后端共用的当前版本，已包含技巧相关步骤；TS 代码内的 `DEFAULT_PROMPTS` 兜底文本不含这些步骤（对比 `backend/workspace/prompts/storyboard_breaker.md:12` 与 `backend/src/agents/index.ts:91-95`）。

## 10. 相关文件索引

| 文件 | 说明 |
|---|---|
| `frontend/app/views/drama/episode.vue` | 工作台（剧本 → 制作 → 导出） |
| `frontend/app/views/drama/detail.vue` | 剧详情（剧集列表、素材库） |
| `frontend/app/utils/shot-versions.ts`、`shot-references.ts` | 版本与参考图纯函数 |
| `backend/src/db/sqlite-schema.ts`、`schema.ts`、`migrations.ts`、`progress-status.ts` | DDL、Drizzle 定义、迁移与造型对账、进度状态推进 |
| `backend/src/agents/index.ts`、`agents/tools/*.ts` | Agent 注册、默认提示词、工具 |
| `backend/src/routes/agent.ts`、`episodes.ts`、`storyboards.ts`、`tasks.ts`、`merge.ts` | 核心接口 |
| `backend/src/services/extraction.ts`、`video-prompts.ts`、`final-prompt.ts`、`character-looks.ts`、`storyboard-looks.ts` | 异步任务与造型服务 |
| `backend/workspace/prompts/`、`backend/workspace/skills/` | 提示词与技能 |
| `docs/superpowers/specs/2026-10-12-character-looks-design.md` | 造型设计文档（270 行） |

## Caveats / Not Found

- 流水线调用生图/生视频/拼接的三个位置已记录（2.3、2.4、2.5），适配器与 FFmpeg 实现未展开（由另一份调研负责）。
- `backend/src/agents/tools/extract-tools.ts` 只读了工具定义骨架与角色保存逻辑（211-287），场景与道具保存逻辑按工具描述与表引用归纳，未逐行核对。
- `backend/src/services/generation.ts` 只核对了状态取值与回写位置，未通读。
- Go 后端只核对了表结构、文件规模与 `agent.go`、技巧相关入口；各接口行为是否与 TS 版逐一一致未验证（任务说明称两者接口兼容）。
- 5.2 中「刷新后如何恢复待生成/失败状态」为推断，未逐行核对。
- HTTP 请求体大小上限（框架层）未检查，第 6 节「无长度限制」仅指业务代码层。
- 未读取任何 `.env` 文件，未修改参考项目任何文件，未执行其 `.wolf` 流程。
