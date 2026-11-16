# 研究：内容组合导出的数据来源与权限

- 问题：章节／场景摘要、可阅读设定、本人聊天、作品笔记和作者区块分别从哪里读取，哪些安全与一致性契约可复用？
- 范围：仓库内部。
- 日期：2026-11-16。
- 当前阶段：planning；用户已选 A（DOCX／Markdown／TXT 组合导出，EPUB 保持现状）。本文是代码事实与技术建议，不代替产品范围审批。
- 操作：只读代码与规范；未运行测试、未改产品代码。

## 研究结果

### 1. 章节摘要与场景摘要

**当前用户界面里的“章节摘要”来自最新一次章节分析。** 阅读器先取分析列表首项，再取详情并显示 `summary`，同时显示分析时间与“正文已修改，分析可能过期”（`frontend/src/components/reader/ReaderAnalysisPanel.tsx:15`、`:18`、`:43`、`:50`）。章节编辑器分析抽屉同样显示摘要和过期提示（`frontend/src/components/chapter/AnalysisDrawer.tsx:470`、`:485`）。

- `Chapter` 没有 `summary`；`ChapterAnalysis` 保存 `summary`、来源 `content_hash`，每次分析一行（`backend/app/models/entities.py:579`、`:853`）。
- “最新”按每章 `max(ChapterAnalysis.id)`，并校验分析的 `project_id` 与章节一致；已有批量原语 `analysis_service.latest_analyses(db, chapter_ids)`，调用方应先取得已授权章节集合（`backend/app/services/analysis_service.py:406`）。
- 过期判断不是简单 `sha256(chapter.content)`：`rich_document_ai.analysis_fingerprints(db, chapters)` 在没有开启作者区块时沿用正文 hash；否则所有开启区块的内容、标题、位置等计入指纹，即使某区块因预算未进入模型也保守失效；格式、高亮及关闭区块不计入（`backend/app/services/rich_document_ai.py:61`）。该函数还核对读取前后 `Chapter.content_revision`，冲突抛 409（`:99`）。
- 读取分析的人工路径要求 `content.read`，且必须是作者或 accepted 协作者；仅管理员人工权限会在 `_task_actor` 被拒绝。分析结果是作品级可见内容，任务引用按真实 actor 隔离，不能把全部 `AnalysisOut` 原样序列化成导出资料（`backend/app/services/analysis_service.py:552`、`:616`、`:644`）。
- `Outline.summary` 是大纲，`ChapterPlan.summary` 是展开后的细纲，并不代表已写正文摘要（`backend/app/models/entities.py:566`、`:622`）。不存在自动将其作为分析摘要 fallback 的现有导出契约。
- 场景 `Scene.summary` 是独立持久字段，可人工修改；场景内容／摘要更新推进章 `content_revision`。场景没有分析来源指纹或“摘要已过期”字段，不能仿造章分析的可靠 freshness 状态（`backend/app/models/scenes.py:11`；`backend/app/services/scene_service.py:529`、`:557`）。拆景新右场景摘要为空，合景采用选定侧的摘要，是元数据选择而非重新生成（`backend/app/services/scene_service.py:622`、`:663`、`:719`）。

技术建议：

1. 整章摘要使用最新 `ChapterAnalysis.summary`，保留“分析时间／可能过期”的来源说明；场景摘要使用已保存 `Scene.summary`。
2. 部分场景导出时，不自动附上整章分析摘要，否则摘要可能包含未选择场景。章与场景的选择语义需要产品方案明确。
3. 导出只读现有结果；缺失摘要不得自动发起 AI，也不把细纲静默冒充摘要。缺失／过期时如何展示属于产品决策。
4. 若要承诺同一版正文与章摘要状态，需在组合服务中核对整次快照或读取代次，不能把多个 HTTP 结果随意拼接。

### 2. 设定资料与现有批量原语

**可读设定需要使用本书的当前实体资料和公开字段值，不能直接 dump 数据表。**

| 内容 | 现有入口／可复用原语 | 注意事项 |
| --- | --- | --- |
| 人物、世界观 | `story_service.list_items`／`list_owned(..., manage=True)`，`backend/app/services/story_service.py:48`、`:54`、`:62` | 本书 `project_id` 过滤与既有 `sort,id` 顺序；人工读使用 `content.read`。 |
| 统一条目选择目录 | `setting_catalog_service.list_entries(..., manual=True)`；内部 `candidate_batch` | 四类 `character/world/organization/career`，世界观再细分 6 类；公开目录是分页，内部游标每批 ≤200（`backend/app/services/setting_catalog_service.py:16`、`:77`、`:146`）。 |
| 已选条目身份与名称 | `setting_catalog_service.get_entries` | 每种实体批量查询，严格本书范围，结果缺项报 404；只投影名称，不展开正文（`backend/app/services/setting_catalog_service.py:196`）。 |
| 组织 | `organization_service.list_orgs(..., manual=True)`、`_outs` | 名称、简介、层级等；上级引用过滤到同作品，成员需单独结构化查询（`backend/app/services/organization_service.py:209`、`:279`）。 |
| 职业 | `career_service.list_careers(..., manual=True)`、`career_outs`／`stages_of` | 职业说明及阶段可批量加载；人物职业挂接另有项目级 `list_project_assignments`（`backend/app/services/career_service.py:79`、`:123`、`:289`、`:956`）。 |
| 人物关系 | `relationship_service.list_relationships(..., manual=True)`、`to_outs` | 只取 `is_present=true`、合法本书角色与全局／本书关系类型，转为两端姓名和称谓；不直接输出 FK（`backend/app/services/relationship_service.py:741`、`:826`）。 |
| 自定义字段 | `setting_field_service.resolve_entry_fields` + `setting_field_formatting.format_entry_fields` | 支持 text／line／dropdown／reference 及 legacy 字段；前者批量求值与公开解码，后者输出具名选项／引用，保留有内容的停用旧值，不递归展开引用目标正文（`backend/app/services/setting_field_service.py:994`；`backend/app/services/setting_field_formatting.py:20`、`:37`）。 |

自定义字段一致性边界：`resolve_entry_fields` 前后核对目录、时间线和 AI 可见性版本；不创建空绑定、不 commit、不 autoflush。跨批调用仍须由组合服务维持整次导出的快照／版本边界（`backend/app/services/setting_field_service.py:1007`、`:1158`；`.trellis/spec/backend/setting-fields.md:5`）。`position=None` 表示所有有效事件后的“当前”，包含计划章事件，不能将其描述成“最新已写章末”（`.trellis/spec/backend/setting-fields.md` 的值契约）。

`story_service.format_characters` 明确是向导参考摘要，只输出部分人物字段，不能直接当完整人物档案导出器（`backend/app/services/story_service.py:401`）。世界观纯文本分组和字段标签可参考既有 formatter，但应由新可读文档模型明确字段白名单，避免泄露 AI 配置等无关字段。

系列资料使用**已采用的本书投影**。`SeriesEntryOut.id` 不是本书实体 ID；未采用系列条目不属于本书目录、正文或备份。共享基础合并后会按本书事件物化，不应绕过投影直接读取系列库（`.trellis/spec/backend/series-settings.md:15`、`:17`；`backend/app/services/series_service.py:728`）。

人工导出不要套用 AI 预算或 AI 隐藏过滤：`ai_hidden`／`never` 是模型输入策略，不是人工资料可见性；应以现有人工读权限为准（`.trellis/spec/backend/setting-fields.md:7`）。导出当前值与历史事件／按章快照是不同产品范围，不能默默扩展到全部历史。

### 3. 本人聊天的读取与最终权限核验

**聊天归实际会话创建者所有，作品作者不能因此读取协作者的私有聊天。**

- `agent.access.lock_agent_entry(..., lock=False)` 允许作品作者或 accepted 成员（含 viewer）读取私人会话；仅管理员、未接受邀请、已撤权均拒绝（`backend/app/services/agent/access.py:92`）。
- `_conversation_entry` 按 `id + project_id + user_id` 三者限定会话（`backend/app/services/agent/service.py:353`）。列表也按 `AgentConversation.user_id == actor_id`（`:522`）。
- viewer 或重新邀请后的旧会话仍可只读；会话邀请轮次用于写能力，不能对只读导出错误要求 `ai.use`、当前邀请轮次或模型授权（`backend/app/services/agent/access.py:112`）。管理员兼有效协作者也只能读取本人会话，管理员角色不扩大私有资料范围。
- `get_conversation` 不是单纯查表：读取消息／提案，冻结会话和消息证据，最后经过 `personal_projection.project_agent_payload`（`backend/app/services/agent/service.py:581`、`:603`、`:620`）。
- 投影在**新的数据库快照**核验账号和 token、作品生命周期、accepted 成员、消息内容、会话归属和创建身份；会话 writable／sealed 状态变化也会失效。正文即使是 JSON 或截断 JSON，也会对私人内部字段进行投影／隐藏（`backend/app/services/personal_projection.py:311`、`:430`、`:559`、`:602`、`:617`、`:731`）。
- `_prepare_committed_changes` 设置 `owner_only=False` 让 accepted 成员经过最终核验；不要自己构造不完整 proofs 并依赖默认 `owner_only=True`（`backend/app/services/agent/service.py:482`、`:504`）。

技术建议：先使用现有本人会话列表作为选择来源；专用导出 helper 复用会话详情的**安全投影过程**，只输出 `role in {user, assistant}` 的可读 `content`，按原消息顺序，配会话标题／角色／时间。排除 `tool`、tool_calls、工具回显、提案参数、page_context、usage、Skill 快照和运行任务结构。

**可以不传 TaskEngine、不生成 TaskOut 而复用最终安全投影。** `personal_projection.project_agent_payload` 的参数只有 `db/user_id/project_id/payload/proofs`（`backend/app/services/personal_projection.py:670`）；`get_conversation` 中唯一需要 engine 的部分是主动加载当前轮次并调用 `task_service.task_out`（`backend/app/services/agent/service.py:591`、`:615`），导出正文不需要走这部分。可在 agent service 内抽取只读 helper，遵循以下现有证据形状：

1. 复用 `_conversation_entry(..., lock=False)` 取得已授权本人会话与 entry。
2. 按 `AgentMessage.id` 读取要导出的已保存 user／assistant 消息；用完整 `MessageOut.model_validate(...).model_dump(mode="json")` 冻结核验证据。`MessageOut` 自身也会去掉 Provider 签名、thinking blocks 和 Skill 正文（`backend/app/schemas/agent.py:73`）。
3. 按详情现有方式冻结 `ConversationOut`、`created_at`、当前 `writable` 和 `history_sealed`；这些是最终身份／授权竞争核对资料，不能用空占位替代（`backend/app/services/agent/service.py:601`）。
4. 构造无提案／无活动轮次的合法 proofs：`changes={}`、`conversations={}`、`tasks={}`、`owner_only=False`、`require_task_proof=False`，并填入真实 `conversation` 与 `messages` 证据；不传 `turn`。`changes` 键是当前实现必需的（`backend/app/services/personal_projection.py:495`），`owner_only=False` 不能漏掉。
5. 将仅含会话标题及消息角色／时间／正文的 payload 交给 `project_agent_payload`，最后从安全结果组装文档。即使输出已缩为正文，证据仍应保留完整 `MessageOut`，因为最终核验会与数据库完整 DTO 比较（`backend/app/services/personal_projection.py:602`）。

这会复用新的数据库快照、消息身份／内容校验和正文中 JSON 内部值过滤；无需查询提案、active turn 或生成 TaskOut。投影可能为正文中真实出现的私人引用查询相关 Task／配置可见性，这是安全过滤的一部分，不是启动任务。该 helper 目前尚不存在，上述为经函数签名与控制流确认的可实现方案，尚未经测试。

直接 SELECT `AgentMessage.content` 再输出会绕过私有值投影和最终授权核验，不能作为简化方案；也不能通过无 proofs 的 `project_payload` 冒充会话／消息身份核验。现有 proofs 的 `conversation` 是单会话结构，多个会话组合需设计最终响应前的整体授权复核／失效行为，不能以逐会话早期验证冒充整份文件最后时刻的原子快照。

### 4. 笔记与作者区块

作品笔记是作品级内容，无 `user_id` 私人归属列（`backend/app/models/notes.py:10`）。列表与详情要求 `content.read`，并按 `project_id` 过滤（`backend/app/services/note_service.py:20`、`:60`）；列表仅返回 180 字摘要，导出不能误用列表 excerpt 当完整笔记（`:44`）。当前顺序为固定优先、更新时间倒序、ID 倒序。按笔记 ID 选择和只导出笔记在数据层可实现，最终导出仍须经过作品 `export` 能力。

作者区块是 `SceneSection`，有稳定 `logical_key`、标题、内容、格式、锚点和活动标记（`backend/app/models/rich_document.py:35`）。人工读取完整场景文档使用 `get_manual_chapter(..., "content.read")`，不要求 `include_in_ai=true`（`backend/app/services/rich_document_service.py:30`）。因此它是作品人工资料，名称中的“作者”不是私有所有权标记。

若纳入资料导出，可按已授权作品／章节／活动场景下的活动 section 定位，保留章节／场景来源，单独成资料段；`include_in_ai` 不决定是否人工导出。`capture_documents` 可批量读出活动区块，但也读取标记和退场数据且要求外层核对章版本，不适合无条件塞进导出路径（`backend/app/services/rich_document_store.py:24`、`:41`、`:67`）。专用 section 读取应严格筛选 active、只取所需列，核对所属场景／章／作品，避免带入退场内容、标记摘录和历史草稿。

当前正文导出明确不包含作者区块和标记（`backend/app/services/rich_document_render.py:1`；`.trellis/spec/backend/rich-documents.md:31`）。是否新增一个默认关闭的“作者区块”资料选项仍需产品决策；不能把“设定、聊天、笔记等”直接解释成全部富文档内部数据。

### 5. 规划需要明确的产品决策

下列为待决项，不是研究代理已做出的选择：

1. 章摘要使用最新分析后，缺失／过期时导出占位、提示还是拒绝；不选择静默回退细纲或自动生成。
2. 章级与场景级选择怎样避免整章摘要泄露未选场景信息；整章摘要与场景摘要是否都可分别勾选。
3. 设定按分类全选还是支持条目逐项；人物关系／组织成员／职业挂接是否作为所选条目的附属资料；不能自动把被引用对象完整档案补进文档。
4. 设定导出当前值还是提供历史／按章时点；当前值需说明可能包含计划章节事件。
5. 聊天按会话选择还是按消息选择；默认关闭、仅本人、只提取安全正文是技术上最稳妥的起点。
6. 作者区块是否纳入首版资料范围；知识库原文、灵感立项会话、标记／历史也没有因 TODO 的“等”字自动获得范围承诺。
7. 是否允许正文选择为空、只导资料；“空集合”应与“全部内容”区分，不能沿用旧导出未勾选即全部的隐式语义而误导用户。

## 相关文件

| 路径 | 用途 |
| --- | --- |
| `backend/app/services/analysis_service.py` | 章节最新分析、人工读取、任务引用隔离。 |
| `backend/app/services/rich_document_ai.py` | 章分析来源指纹，包含开启作者区块。 |
| `backend/app/services/scene_service.py` | 场景摘要和结构版本。 |
| `backend/app/services/setting_catalog_service.py` | 四类设定目录与批量身份投影。 |
| `backend/app/services/setting_field_service.py` | 字段当前值／时点值及一致性检查。 |
| `backend/app/services/setting_field_formatting.py` | 自定义字段可读标签和值。 |
| `backend/app/services/agent/service.py`、`agent/access.py` | 本人会话范围和详情安全投影。 |
| `backend/app/services/personal_projection.py` | 新快照最终鉴权、消息证据与内部值投影。 |
| `backend/app/services/note_service.py` | 作品共享笔记的分页／完整读取。 |
| `backend/app/services/rich_document_service.py`、`rich_document_store.py` | 作者区块的人工读与完整文档捕获。 |

## 相关规范

- `.trellis/spec/backend/scenes.md`：Scene 正文权威、章兼容投影、活动身份与顺序。
- `.trellis/spec/backend/rich-documents.md`：正文格式、作者资料隔离、Unicode 坐标与导出规则。
- `.trellis/spec/backend/setting-fields.md`：四类字段、当前时点、引用名称与批量一致性。
- `.trellis/spec/backend/series-settings.md`：已采用本书投影与未采用系列资料边界。
- `.trellis/spec/backend/mention-tracking.md`：笔记与来源定位；其中部分旧聊天说明已落后于协作者实现，应以当前 agent/access/service 代码为准。
- `.trellis/spec/backend/project-transfer.md`：备份与可阅读导出职责不同；智能体运行数据不随项目 JSON 迁移。

## 相关测试（仅定位，未执行）

- `backend/tests/test_rich_document_analysis.py`：开启区块参与分析 freshness。
- `backend/tests/test_rich_document_render.py`：正文导出不加载作者区块／标记，格式与正文一致。
- `backend/tests/test_collaboration_analysis_task_visibility.py`、`test_collaboration_analysis_task_visibility_postgresql.py`：分析可见性与私人任务引用。
- `backend/tests/test_agent_entries_e4id.py`、`test_agent_entries_e4id_postgresql.py`：本人会话、协作者／viewer／重邀／管理员入口。
- `backend/tests/test_agent_model_value_privacy.py`、`test_agent_preview_guard_privacy.py`：内部值投影。
- `backend/tests/test_collaboration_content_access.py`、`test_collaboration_story_access.py`：共享人工内容与导出能力。
- `backend/tests/test_setting_fields.py`、`test_setting_fields_postgresql.py`、`test_series_transfer.py`：字段公开解码和本书系列投影。
- `backend/tests/test_notes.py`：笔记完整内容、归属与 revision。

## 外部参考

本研究只核对本仓库，不涉及第三方库选择。DOCX 库、文件兼容性与渲染工具由主会话另行研究。

## 限制与未发现项

- 没有发现可直接覆盖所有资料类型的“可阅读组合导出”服务；现有 formatter 多为 AI 参考摘要，不能承诺完整档案。
- 没有独立 `Chapter.summary`、场景摘要 freshness、现成的聊天文本导出 helper。
- 未运行测试；以上是当前源码事实与实现约束，不是运行时验收结果。
- 当前聊天协作者语义比旧 `mention-tracking.md` 中“只对 owner 开放”的说明更宽；实现时应继承最新 actor 隔离代码，不能照旧规范退回作者专属或放宽为全书聊天。
