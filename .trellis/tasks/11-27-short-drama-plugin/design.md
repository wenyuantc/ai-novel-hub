# 短剧插件（文本段）：技术设计

> 需求见同目录 `prd.md`（S1–S12）；决定编号见父任务 `prd.md`（D9、D12、D18）。调研依据：`research/reference-drama-pipeline.md`。
> 框架契约以工作区里已实现的代码为准：`backend/app/plugins/manifest.py`、`frontend/src/plugins/types.ts`。

## 1. 设计原则

1. **按功能重新实现，不搬代码。** 参考项目是单用户工具、技术栈不同；这里对齐它的流程与数据形态，但每一处都要加上归属、协作权限、并发保护与备份。
2. **结构化输出代替工具调用循环。** 参考项目的四个 Agent 靠模型调用「保存」工具写库（调研 4.3–4.6）。这里改为模型输出 JSON、
   由服务端校验后写库：更容易校验、测试与控制费用，也不依赖模型的工具调用能力。
3. **插件自成一体。** 表、接口、任务、提示词、页面都在插件目录内；核心只多出框架层面的通用接入点。
4. **AI 结果落库前核对版本。** 所有可编辑文本带修订号；AI 任务结束写库时若内容已被人改过，结果作为待确认草稿保留，不覆盖。

## 2. 结构

```
backend/app/plugins/drama/
  __init__.py        # MANIFEST
  models.py          # 全部 drama_* 表
  schemas.py
  api/               # dramas.py、episodes.py、assets.py、shots.py、export.py
  services/          # drama_service、episode_service、asset_service、shot_service、linking、export
  prompts/           # 四组提示词（Jinja2 模板文件）与输出解析
  styles.py          # 9 种风格预设
  handlers.py        # drama.rewrite / drama.extract / drama.storyboard / drama.prompts
  transfer.py        # 作品备份规则
backend/alembic/versions/0083_drama_text.py      # 编号以实施时的链尾为准

frontend/src/plugins/drama/
  index.ts           # 插件对象：页面与导航
  api.ts、types.ts
  pages/             # DramaListPage、DramaDetailPage、EpisodeWorkbenchPage
  components/        # 按步骤拆分：SourceStep、ScriptStep、AssetStep、ShotStep …
```

登记：后端在 `app/plugins/builtins.py` 的 `REGISTERED` 加一行；前端在 `src/plugins/catalog.ts` 的 `PLUGINS` 加一行。
插件标识 `drama`，接口前缀 `/api/v1/plugins/drama`，任务类型前缀 `drama.`，页面路径 `features/drama/...`。

## 3. 本子任务需要给框架补的接入点

框架目前只支持接口与后台任务（`PluginManifest` 只有 `routers`、`task_handlers`）。短剧需要再加两项，都做成通用机制：

| 接入点 | 做法 |
| --- | --- |
| 作品备份规则 | `PluginManifest` 增加 `transfer_rules: tuple[TableRule, ...]` 与 `not_exported: dict[str, str]`；每个应用装配自己的 `transfer_catalog`，与核心规则合并。插件还提供 `transfer_snapshot` 和 `transfer_validator`，分别处理导出期间编辑与导入时跨行约束。规则与插件是否启用无关：停用时数据仍在，备份仍应带上。 |
| 作品彻底删除 | 不加钩子。`drama_*` 表经外键 `ON DELETE CASCADE` 挂在 `projects` 下，随作品删除。 |

这两项改动连同测试在本子任务内完成，并补进框架规范。

## 4. 数据模型

所有表以 `drama_` 为前缀。可编辑的主体表带 `revision`（整数，每次修改 +1，用于并发保护）。

### 4.1 剧与集

`drama_series`（剧）：`id`、`project_id`（外键 CASCADE）、`title`、`style`（风格预设 key）、`aspect_ratio`（16:9 / 9:16 / 1:1）、
`description`、`sort`、`revision`、时间戳。

`drama_episodes`（集）：`id`、`series_id`（CASCADE）、`number`（剧内唯一）、`title`、`source_text`（原始内容快照）、
`script`（格式化剧本）、`script_skipped`（是否跳过改写）、`resolution`（480p / 720p / 1080p，供后续子任务使用）、
`status`（`draft` / `active` / `completed`）、`revision`、时间戳。

`drama_episode_sources`：`episode_id`（CASCADE）、`chapter_id`（外键 SET NULL）、`chapter_title`、`sort`。
只记录「这一集取自哪些章」，正文在建集时复制进 `source_text`；之后章节再改不影响已建的集（S2）。

### 4.2 资产

`drama_characters`：`id`、`series_id`、`name`、`role`、`description`、`appearance`（样貌，各造型共用）、`sort`、
`novel_character_id`（外键 → 小说角色，SET NULL）、`revision`。剧内 `name` 唯一。

`drama_looks`（造型）：`id`、`character_id`（CASCADE）、`name`、`styling`（妆造）、`image_prompt`、`is_default`、`sort`、`revision`。
每个角色至少一套、恰有一套默认；同角色下造型名唯一（调研 5.5）。

`drama_scenes`：`id`、`series_id`、`location`、`time_of_day`、`description`、`lighting`、`image_prompt`、
`novel_world_setting_id`（外键 → 小说世界观条目，SET NULL）、`revision`。剧内 `(location, time_of_day)` 唯一。

`drama_props`：`id`、`series_id`、`name`、`category`、`description`、`image_prompt`、`revision`。剧内 `name` 唯一。

集与资产的关联（本集用到哪些）：`drama_episode_looks`、`drama_episode_scenes`、`drama_episode_props`，各为两列复合主键。
角色是否出场由其造型是否关联推出，不单设集与角色的关联表。

图片相关的列与表在 `11-27-short-drama-media` 增加，本子任务不建。

### 4.3 分镜

`drama_shots`：`id`、`episode_id`（CASCADE）、`position`（集内顺序）、`title`、`description`（画面描述，按【镜头N】分子镜头，台词写在其中）、
`shot_size`（景别）、`atmosphere`、`duration_seconds`（2–30）、`scene_id`（SET NULL）、`video_prompt`、`revision`、时间戳。

`drama_shot_looks`（分镜引用的造型，同一角色可多行即镜头内换装）、`drama_shot_props`：两列复合主键。

顺序用 `position` 稀疏整数；拖动排序一次请求提交整集的新顺序并核对集的修订号。

### 4.4 与小说设定的关联（D12、S5）

`services/linking.py`：提取完成写库前，按名称（含小说角色的别名）匹配本作品的角色，按地点名匹配世界观里类别为地点的条目。
命中则写入关联 id，并在该资产的 `appearance` / `description` 为空时带入小说设定的对应文本作为初值。
之后短剧里的修改只改短剧表；小说设定删除后外键置空，短剧资产保留。界面在资产卡片上显示「已关联小说角色 XXX」，可手动改关联或解除。

## 5. AI 任务

四种任务类型，均登记在插件清单里：

| 类型 | 输入 | 输出 | 资源键（同键互斥） |
| --- | --- | --- | --- |
| `drama.rewrite` | 集的原始内容、风格 | 格式化剧本（纯文本，流式可见） | `drama.episode:{id}` |
| `drama.extract` | 剧本、剧内已有资产清单、可关联的小说设定清单 | 角色（含造型）、场景、道具的 JSON；可只提取其中一类 | `drama.episode:{id}` |
| `drama.storyboard` | 剧本、本集资产（名称与 id） | 有序分镜 JSON | `drama.episode:{id}` |
| `drama.prompts` | 目标清单（若干造型 / 场景 / 道具 / 分镜） | 各目标的图片提示词或视频提示词 | `drama.episode:{id}`（剧级素材库发起时为 `drama.series:{id}`） |

- 同一集同时只允许一个短剧任务，避免改写与拆分镜互相踩。
- 提示词是插件内的 Jinja2 模板文件，内容以参考项目的四份中文提示词与技能为蓝本改写（调研 4.3–4.6 列出的硬规则全部保留：
  剧本格式、去重规则、道具筛选、造型判定、分镜时长与数量锚定、台词时长下限、三种图片版式、视频提示词版式）。
  本期不进入核心的提示词模板注册表，作者不能覆盖。
- 输出解析：`drama.rewrite` 直接取文本；其余三种要求模型输出 JSON，用 Pydantic 校验；校验失败时带着错误信息重试一次，仍失败则任务失败并保留原始输出供排查。
- 长内容：拆分镜与提取都可能超出单次输出上限。分镜按剧本的场景分批生成（每批若干场景），批次间把已生成的镜号与时长累计传下去；
  任务用 `checkpoint` 记录已完成的批次，中断后重试从断点继续（做法参照批量生成，`.trellis/spec/backend/batch-generate.md`）。
- 写库时机：任务成功的终态事务内写入业务表，并核对目标的修订号。修订号已变（任务运行期间有人改过）时不写业务表，
  结果留在任务的 `result` 里，界面提示「内容已被修改，是否采用 AI 结果」，由人确认后通过普通保存接口写入。
- 重新拆分镜会替换该集全部分镜，受理前由界面二次确认；提取是合并而不是替换（已有资产按去重规则更新描述，不删除）。
- 协作与模型：处理器按现有处理器的约定实现受理预检、执行身份与协作执行三项声明
  （参照 `backend/app/services/task_engine/handlers/entities.py` 的 `admission_preflight_aware`、`execution_identity_aware`、`collaboration_execution_ready`），
  使编辑者在获得作者模型授权时可以发起任务（S9）。这是本子任务里与核心耦合最深的部分，实施计划单列一步并先读规范。
- 用量：经现有调度层调用，自动计入 AI 用量与预算，`kind` 记为任务类型。

### 5.1 已核实的执行引擎约定

- 受理路由通过 `EditorUser` 核对页面账号，并在锁外预检文本模型。引擎在写事务内消费受理证明，冻结原模型、操作者和协作邀请轮次。重试复核原模型，已生成的场景批次还要核对原始输入签名。
- 领取后用原执行身份建立 `collaboration_ai_guard.for_task`。同一守卫控制真实模型发送、用量计费、SSE 发布与成功写入。任务只向模型传已选择的作品材料，关联小说设定按当前 AI 可见性过滤。
- 分批任务把已验证的批次和材料签名写入 Task 断点。上一轮身份在提交后按已知断点推进；协作者滞后的 SSE 帧只允许在同一执行内使用内容前缀不变的后继批次证明。
- 成功回调在引擎持有的同一个数据库事务中写业务表并推进修订号。校验失败则回滚业务变更，按原执行身份释放活动资源。发送或输出时权限被撤销时不会保存剧本、资产和分镜。
- 成功事件在 `data.result` 中返回 `applied` 和 `draft`。发生版本冲突时采用动作须验证原任务 UUID、作品写权限及冲突时观察到的版本；人工再次修改后须重新生成。

## 6. 接口

前缀 `/api/v1/plugins/drama`，框架已在挂载处加了「插件已启用」的门禁。作品级鉴权用现有的作品访问判定：读需 `content.read`，
写需 `content.edit`，发起 AI 任务需 `ai.use`，导出需 `export`（`backend/app/services/project_access.py:17`）。无权或不存在一律 404。

| 资源 | 接口 |
| --- | --- |
| 剧 | `GET/POST /projects/{pid}/series`，`GET/PATCH/DELETE /series/{sid}` |
| 集 | `GET/POST /series/{sid}/episodes`（建集时带章节 id 列表或直接给文本），`GET/PATCH/DELETE /episodes/{eid}` |
| 资产 | `GET /series/{sid}/assets`（素材库），`GET /episodes/{eid}/assets`；角色、造型、场景、道具各自的增改删；集与资产的关联增删；小说设定关联的改与解除 |
| 分镜 | `GET /episodes/{eid}/shots`，`POST`（单条新增），`PATCH/DELETE /shots/{id}`，`PUT /episodes/{eid}/shots/order` |
| AI | `POST /episodes/{eid}/tasks`（`kind` 与参数），返回任务；进度与取消走核心的任务接口 |
| 导出 | `GET /episodes/{eid}/export?format=docx|md&parts=script,shots` |
| 字典 | `GET /styles`（风格预设） |

- 所有 `PATCH` 带 `revision`；过期返回 409 `stale_revision`，前端保留草稿并提示（项目既有模式）。
- 上限校验见 `prd.md`「细节取值」；超限返回 400 并说明。
- 导出：组装文档模型后调用现有的渲染函数（`backend/app/services/document_export/service.py:56`），下载约定与现有导出一致。

## 7. 前端

- 插件对象在 `src/plugins/drama/index.ts`：三个作品内页面，导航入口「短剧」放进作品导航的新分组「改编」。

| 路径（挂在 `projects/:pid/features/` 下） | 页面 |
| --- | --- |
| `drama` | 剧列表：新建剧（标题、风格、画幅） |
| `drama/:sid` | 剧详情：剧集列表、素材库两个标签页 |
| `drama/:sid/episodes/:eid` | 集工作台：左侧步骤栏，主区按步骤切换 |

- 集工作台的步骤（本子任务交付前四步的文字部分）：原始内容 → 剧本 → 资产 → 分镜。「视频制作」「导出」「画布」的入口在后续子任务加入。
  参考项目把工作台写成一个 6600 多行的文件（调研 8.1）；这里按步骤拆成独立组件，状态用本地状态加请求钩子，不新建全局 store。
- 原始内容步骤：章节多选器（按卷分组、显示字数、连续性校验）加文本框。
- 剧本步骤：发起改写后用现有的流式任务钩子显示实时输出（`useTaskStream`，见前端 Hook 规范），可取消；完成后进入可编辑状态。
- 资产步骤：角色卡片（含造型条）、场景、道具三栏；提取按钮；关联小说设定的标记与编辑。图片位置在本子任务里显示占位。
- 分镜步骤：左侧分镜列表（拖动排序、新增、删除），右侧检查器编辑描述、景别、氛围、时长、引用的场景 / 造型 / 道具、视频提示词。
  视频提示词输入支持 `@名称` 引用已绑定素材的着色提示。
- 只读：查看者进入时所有编辑控件禁用、AI 按钮隐藏；无模型配置时 AI 按钮给出去配置的提示（复用 `useHasAIConfig`）。
- 自动保存与冲突：文本类字段失焦保存；409 时保留本地内容并提供「重新载入 / 覆盖保存」。

## 8. 备份（S11、D18）

- `transfer.py` 为全部 `drama_*` 表声明导出规则：`project_id` 回填、表间外键重映射、指向小说角色与世界观条目的外键重映射到导入后的新 id。
- 后续子任务加入的媒体引用列在规则里声明为不导出，导入后为空。
- 作品备份格式版本由 18 升到 19；旧版本备份导入时没有短剧部分，按空处理；向下兼容测试沿用现有各版本样例。
- 导出备份时，若作品含短剧媒体（后续子任务），导出对话框提示「短剧的图片与视频不包含在备份中」；本子任务先把提示位留好。

## 9. 停用行为（S12）

框架负责接口、任务、页面与导航的门禁。插件自身只需保证：没有任何核心代码路径读写 `drama_*` 表；
作品备份与作品删除在插件停用时照常处理这些表。

## 10. 权衡记录

| 问题 | 选择 | 放弃的方案与原因 |
| --- | --- | --- |
| AI 写库方式 | 输出 JSON、服务端校验后写 | 模型调用保存工具（参考项目）：依赖工具调用能力，难校验，多轮调用费用高 |
| 一集的来源 | 建集时复制章节正文为快照 | 始终实时读取章节：章节一改，已生成的剧本与分镜就对不上 |
| 提示词可否覆盖 | 本期不可覆盖 | 并入核心提示词模板：要让框架贡献模板键并过核心的对照测试，范围扩大 |
| 分镜顺序 | 稀疏 `position` + 整集排序接口 | 链表或小数排序键：实现复杂，整集重排的场景并不频繁 |
| 资产层级 | 剧级资产 + 集级关联（同参考项目） | 资产挂在集下：跨集无法复用角色造型 |
| 台词 | 写在分镜描述里（同参考项目） | 独立台词字段：视频模型直接消费描述文本，拆开后还得再拼回去；本期没有字幕需求 |

## 11. 规范与文档

- 新增 `.trellis/spec/backend/drama.md`（数据模型、任务契约、输出校验、关联规则、备份规则、错误矩阵、必需测试），前端规范增补短剧页面模式。
- 更新 `project-transfer.md`（v19 与插件规则并入）、框架规范（清单新增字段）。
- README：功能概览增加「短剧插件」，并说明需管理员启用。
