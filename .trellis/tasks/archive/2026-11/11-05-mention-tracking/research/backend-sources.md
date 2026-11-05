# Research: 全文提及的后端来源、权限与索引约束

- Query：核实正文、聊天、知识库与笔记来源；识别精确定位、写入失效、世界观别名扩展及索引方案的约束。
- Scope：internal；只读产品代码，只写本文档，不决定尚待用户确认的范围。
- Date：2026-11-05
- Task：`.trellis/tasks/11-05-mention-tracking`，当前为 planning。
- 范围更新：用户随后回复「全部做」，主会话确认本轮包含正文、项目聊天、新增独立作品笔记、知识库资料四类来源；下面对“缺少笔记”的描述是实施前事实，不是延期建议。

## Findings

### 1. 来源和权限不能只由 project_id 决定

| 来源 | 实际持久化文本与身份 | 现有权限及使用边界 |
|---|---|---|
| 章节正文 | `Chapter.content`，稳定章节 ID、当前 `chapter_no`，`backend/app/models/entities.py:478` | 人工读取使用 `get_manageable_project`；管理员可读他人正文，但普通用户仍 404。子资源必须同时校验 project_id，见 `backend/app/services/chapter_service.py:57`、`:65`、`:72`。 |
| 项目聊天 | `AgentMessage.content`，归于 `AgentConversation`；会话有 project_id 和 user_id，见 `backend/app/models/agent.py:12`、`:30` | `owned_conversation` 同时验证项目所有权、会话项目和会话 user_id，管理员不能继承作品人工管理权限读取聊天，见 `backend/app/services/agent/service.py:91`、`:121`。 |
| 知识库资料 | `KbDocument.text` 是上传文件解析后的全文，不保存原文件；`KbChunk` 是派生分块，见 `backend/app/models/entities.py:877`、`:902` | 文档列表与分块可人工管理读取，见 `backend/app/services/kb_service.py:90`、`:98`、`:214`；上传、重建和模型检索仍严格所有权，见 `:111`、`:199` 与 `.trellis/spec/backend/admin-projects.md`。 |
| 灵感补充想法 | `InspirationSession.state.notes: list[str]`，见 `backend/app/schemas/inspiration.py:59`；会话的 project_id、converted_project_id 均可空，见 `backend/app/models/entities.py:834` | 会话私有，以 `get_owned_session` 校验；追加及删除按数组下标，见 `backend/app/services/inspiration_service.py:162`、`:310`、`:323`。不能自动当成稳定作品笔记实体。 |
| 剧情记忆/实体备注 | `StoryMemory.content` 有手工与自动两类；各类关系、成员、版本等还有 note 字段，见 `backend/app/models/entities.py:918` | 这些是带领域语义的现有记录，不等于第 9 项自由笔记；是否额外追踪需明确范围。 |

权限底座的实际区别：`get_owned_project` 严格检查 user_id；`get_manageable_project` 为人工内容开放 projects.read/edit 能力；二者均检查作品生命周期，见 `backend/app/services/project_service.py:28`、`:39`。因此统一提及接口必须逐来源施加权限；管理员视图里的聊天片段、数量和热力图也不能泄漏私有会话存在性。回收站或删除中的作品不能因为索引记录仍存在而继续可读。

### 2. 「聊天」包含自然语言消息和内部工具载荷

- 自然语言 user 消息在任务创建时保存，见 `backend/app/services/task_engine/handlers/agent.py:43`；assistant 消息每轮将完成的文本及 tool_calls 分开落库，见 `backend/app/services/agent/runner.py:451`。流式未落库片段不属于当前数据库语料。
- tool 消息的 content 是 `tool_result(data)` 包装后的 JSON，可包含读取回来的整段正文、设定、搜索结果；见 `backend/app/services/agent/runner.py:45`、`backend/app/services/agent/context.py:40`。把它全部计为聊天提及会因工具回显重复放大统计。
- `MessageOut` 返回 role/content，限制 tool_calls 只暴露 id/name/arguments，隐藏 provider thinking/signature；Skill 快照只返回 ID/名称，见 `backend/app/schemas/agent.py:65`。不要递归扫描 ORM 的 tool_calls、page_context、skill_snapshot、usage 或生成日志来凑齐提及。
- 前端会把工具消息转换成操作记录，并显示部分内容；`buildAgentTimeline` 的 role 分流见 `frontend/src/utils/agent.ts:60`。因此“用户可看到”与“应纳入自然语言聊天统计”不是同一件事，最终设计应写出是否只含 user/assistant content。
- 会话详情当前一次返回全部 messages，按 message ID 排序，见 `backend/app/services/agent/service.py:159`。会话删除同时受进行中任务约束，见 `:233`；现有消息表对 conversation 外键 CASCADE，索引应能随来源删除失效。

### 3. 没有独立自由笔记，知识库也不是在线笔记编辑器

- 在 models/schemas/api 的 notes、snippet、笔记检索中，找到的是灵感会话补充想法、领域备注和剧情记忆，未发现独立作品自由笔记实体及 CRUD；与 `TODOS.md` 第 9 项现状一致。
- 知识库创建入口是 multipart 文件上传，解析结果写 `KbDocument.text`，见 `backend/app/services/kb_service.py:111`、`:136`。`PATCH /documents/{id}` 只接受 inject 开关，见 `backend/app/api/v1/kb.py:57`、`backend/app/schemas/kb.py:58`；没有手写创建或编辑全文接口。
- 当前读入口是文档元数据列表和分页分块：`backend/app/api/v1/kb.py:37`、`:77`。`KbDocumentOut` 没有全文，`KbChunkOut` 有 content/start_offset/end_offset，见 `backend/app/schemas/kb.py:34`、`:62`。
- 知识库 UI 目前只打开 `KbChunksDrawer`，见 `frontend/src/pages/project/KnowledgePanel.tsx:296`；未发现既有“文档 ID + 原文偏移”导航入口。接入后需要补充来源定位能力，不能仅复用上传页就认为完成反向导航。
- 知识库分块会重叠最多 80 字，见 `backend/app/services/kb_parser.py:43`、`:562`；提及计数应基于原文或按 document_id + 原文区间去重，不能直接加总 KbChunk / RetrievalChunk 命中。

### 4. 定位可以复用码点与内容 hash 思路，不能复用版本 ID 当永久修订号

- 后端 Python str 偏移是 Unicode 码点，选区改写以 `[start, end)` 切片验证；见 `backend/app/services/rewrite_service.py:84`、`:220`。前端已有 UTF-16 ↔ 码点转换，见 `frontend/src/utils/textOffset.ts:1`、`:13`、`:24`，适合提及定位复用。
- `analysis_service.content_hash` 对原始 UTF-8 内容计算 SHA-256，见 `backend/app/services/analysis_service.py:93`。智能体替换预览使用全章 hash，执行时又按原文 CAS，见 `backend/app/services/agent/tools/write_text.py:39`、`:59`。
- 章节自身没有持久 content revision/hash 列，ChapterOut 只有正文及 updated_at 等，见 `backend/app/models/entities.py:478`、`backend/app/schemas/chapter.py:34`。仅凭 updated_at 无法严格判定正文修订：它也会因标题、章号等变化而改变。
- `ChapterVersion` 的最新 manual 版本在 10 分钟内会原地改写；空白正文不写版本；未标星旧版本会裁剪。因此 version_id/seq 不是每次正文编辑唯一且永久的定位依据，见 `backend/app/services/version_service.py:1`、`:71`、`:114`。
- 建议最终定位契约至少明确：source_type/source_id、原文码点 start/end、匹配词、来源内容指纹；导航时验证区间仍对应同一原文，变化时重新定位或显式提示失效。这里是设计建议，尚不是已批准 API。
- Markdown 聊天或解析资料的原始文本偏移不能直接当渲染 DOM 字符偏移；是否在原文层定位，或映射到渲染节点，需要前端设计明确。

### 5. 索引失效入口不止手工章节保存

| 写入类别 | 关键入口/证据 | 对派生索引的影响 |
|---|---|---|
| 手动建章/保存/删除/改章号 | `backend/app/services/chapter_service.py:92`、`:172`、`:234` | 正文变更与版本同事务；删章需清来源；改章号改变时间线与热力图排序，不一定改变正文。 |
| AI 整章生成/重写、批量底层保存 | `backend/app/services/generation_service.py:382` | 生成完成才原子保存 content、word_count、version；不能靠章节 PATCH 钩子覆盖。 |
| 局部改写采纳 | `backend/app/services/rewrite_service.py:220` | 同事务替换正文并写 rewrite 版本。 |
| 版本恢复 | `backend/app/services/chapter_version_service.py:73` | 恢复旧正文后创建 restore 版本，必须重新识别当前提及。 |
| 智能体正文替换 | `backend/app/services/agent/tools/write_text.py:51` | 使用 SQL 条件 UPDATE，不保证触发基于 ORM 属性的更新监听。 |
| 智能体撤销 | `backend/app/services/agent/effects.py:379`、`:448` | 直接恢复表行/章节版本，需覆盖撤销后的来源与词典变化。 |
| 拆书导入 | `backend/app/services/book_import/service.py:314`、`:347` | 批量插入正文并 add_initial 版本，可一次 3000 章；无逐章 PATCH。 |
| 项目备份导入 | `backend/app/services/project_transfer/importer.py:922`，以及 `.trellis/spec/backend/project-transfer.md` | 通用表导入与派生检索重建；新索引需要明确懒重建或同事务/后台重建策略。 |
| 角色/世界观创建、改名、别名修改、删除 | `backend/app/services/story_service.py:101`、`:123`、`:161` | 名称词典变更可能影响全书所有来源；不只是变更实体当前出现的章节。 |
| 聊天消息追加/会话删除、知识库上传/删除/重建 | `backend/app/services/agent/runner.py:45`、`:464`；`backend/app/services/agent/service.py:233`；`backend/app/services/kb_service.py:111`、`:189`、`:199` | 按稳定原文来源失效；知识库重建会删除旧 chunk ID，但文档全文可不变。 |

设定批量生成、卡片导入、拆书提取和智能体工具还会调用或绕过普通 CRUD；最终若选择写入式索引，需要按“原文源与词典源”列出完整入口矩阵，不能只给某一个 service 打补丁。只读查询不应为了索引刷新意外提交当前请求中别的未提交业务修改。

### 6. 地点/物品别名的兼容约束

1. **模型与 API**：地点、物品都是 WorldSetting 的 category；Create/Update/Out 见 `backend/app/schemas/story.py:32`。已有可复用的 `Aliases` 清洗规则（去首尾空格、去空、保序去重、每项 ≤50、最多 10），见 `:10`、`:14`、`:29`。PATCH 应保留“省略/null 不改、[] 清空”的已有语义。是否对所有世界观分类开放别名，是 UI/范围决策。
2. **迁移**：现有最新迁移为 `0031_setting_progressions.py`；新 JSON 列应同时在 ORM 与 Alembic 给旧数据空列表默认，遵循 PostgreSQL/SQLite 双库与外键约束。新增迁移需同步 `project_transfer/exporter.APP_SCHEMA`，见 `.trellis/spec/backend/database-guidelines.md` 与 `.trellis/spec/backend/project-transfer.md`。
3. **项目备份**：`world_settings` 已在 RULES 中，但还没有 json_plain。新增 aliases JSON 必须登记，否则反射门禁失败，见 `backend/app/services/project_transfer/registry.py:1`、`:133`。旧备份缺默认字段可接受，见 `backend/app/services/project_transfer/importer.py:327`、`:342`；通用 JSON 校验只保证 list[str] 形状，见 `:280`，不自动执行 Aliases 的长度/清洗规则，设计需明确是否补充导入约束。
4. **卡片**：当前 CardFile 只有 characters/organizations/relationships/careers，没有 world settings，见 `backend/app/schemas/card.py:107`。因此仅给世界观补别名不必擅自扩充卡片格式；角色别名已有卡片入口且当前 CardCharacter 允许 max_length=50 列表，见 `:54`，新增统一词典读取要兼容既有导入数据，不能假设所有记录都已走 CRUD 的 10 项约束。
5. **设定时间线**：名称/别名属于当前全书元数据，不在剧情变化字段集；character 只跟踪 profile 等字段，world 只跟踪 content，见 `backend/app/services/setting_progression_resolver.py:60`。世界观别名不应无意变成按章节生效字段；如用户想按剧情改名，属于新的产品选择。
6. **revision 与元数据**：基础值编辑模式通过 metadata_changed / `_claim_revision` 保护同页的全书元数据，见 `backend/app/services/story_service.py:136`、`backend/app/services/setting_progression_service.py:608`、`:657`。别名表单应保留该版本守卫和冲突草稿逻辑。
7. **智能体**：世界观创建/修改工具直接使用 WorldSettingCreate/Update，见 `backend/app/services/agent/tools/write.py:458`；改 schema 会同步扩大工具字段。effects 快照按表列读取，见 `backend/app/services/agent/effects.py:138`、`:149`，应回归新字段撤销以及旧效果兼容，不能只测 HTTP 保存。

### 7. 查询时扫描与持久化索引：有据可查的取舍

| 方案 | 优点 | 必须承担的代价 |
|---|---|---|
| 查询时从当前原文扫描 | 无遗漏写入口造成的陈旧索引；词典改名立即可见；便于先定义匹配、权限、区间与计数口径 | 每次统计需读取和扫描大量文本；分页结果也可能要求先扫描全量；不能在 async 请求事件循环里无界 CPU 扫描；需要范围限定、分批及真实规模基准。 |
| 持久化 source/mention 派生索引 | 正反向定位与热力图可以分页聚合；重复查询不重复扫描全文 | 要维护词典 revision、来源指纹、删除清理、首次全量回填、重建状态及并发失效；改名可影响全项目；须覆盖上表全部入口，或以可靠的读时指纹核查补齐。 |
| 按来源懒重建/缓存 | 有机会平衡首版侵入程度和重复查询成本 | 缓存身份必须包含项目、可见来源范围、词典版本和来源版本；跨用户结果不可共享泄漏；“已缓存”不能冒充“全量已扫描”。 |

规模证据：拆书允许 500 万字、3000 章，见 `backend/app/services/book_import/splitter.py:35`；知识库每项目最多 200 文档，每份解析结果可到 300 万字，见 `backend/app/services/kb_service.py:41`、`backend/app/services/kb_parser.py:36`。这些是已有上限，不是本轮测得的实际用户规模或性能；本次未运行性能基准，不能宣称同步全书扫描一定足够快。

已有 `RetrievalChunk` 是 BM25/向量的检索单元，只覆盖 kb/memory，没有精确实体区间、章节正文和聊天来源，见 `backend/app/models/entities.py:949`。它的 version 与缓存失效设计可参考，不能把分词检索结果直接当精确名称提及。若新增项目相关派生表，应在备份 RULES 或 NOT_EXPORTED 明确登记及重建语义；现有 kb_chunks/retrieval_chunks 采用不导出、由原文重建，见 `backend/app/services/project_transfer/registry.py:65`。

### 8. 新增最小作品笔记：权限、备份与审计的接入点

- **业务模型建议**：主会话拟用 `ProjectNote / project_notes(id, project_id, title, content, revision, created_at, updated_at)`，与既有作品资料模式兼容；revision 建议默认 1、PATCH 原子比较 expected_revision 并递增，冲突返回 409 且保留客户端草稿，可参考 `backend/app/services/setting_progression_service.py:91` 的 CAS 思路。如最终确认要收藏/固定，再增加对应字段。项目 FK 应 CASCADE，项目生命周期仍由上层 helper 控制。不要将自由笔记塞进灵感 state.notes 或自动 StoryMemory，以免数组位移、私有会话归属和自动重分析规则污染笔记语义。笔记不自动进入 AI 上下文，不为追踪引入向量/模型任务。
- **权限复用而非新增管理动作**：已有 `projects.read` 明确覆盖全站作品人工内容，`projects.edit` 覆盖子资源增删，见 `backend/app/core/permissions.py:24`。按作品人工资料模式，列表/详情调用 get_manageable_project；所有写入先 `get_manageable_project(..., lock=True, permission="projects.edit")`，随后按 `(project_id, note_id)` 查资源；仍传真实 actor_user_id。可参考 `backend/app/services/memory_service.py:242`、`:301`、`:324`、`:353` 的人工 CRUD 和 `backend/app/api/v1/memories.py:26` 的分页路由，但不要复制自动记忆、章节号或向量排队副作用。是否让笔记比其他作品资料更私有，若有需求必须另外确认，不能通过“项目聊天私有”顺手推导。
- **注册项**：新增 ORM model 要加入现有 models 导出/metadata，API router 加入现有 v1 聚合路由，数据库增加迁移；这些与既有模块同样接入。无需新增 `notes.read/edit` 管理权限目录，否则需额外处理内置角色和持久目录升级，超出最小笔记所需。
- **项目备份**：笔记原文是核心数据，应进入 `project_transfer.registry.RULES`，以 project_id 作为 fill；不存在 JSON/引用字段时普通列即可原样导出。通用导入会分配新主键并填新 project_id，见 `backend/app/services/project_transfer/importer.py:918`。新增派生 mention/source 表则可进 NOT_EXPORTED、由四类原文懒重建；不要把私有聊天纳入项目备份，现有 agent 三表明确不导出（registry.py:68）。补覆盖反射检查、旧备份无笔记、笔记文本往返、派生表不随备份迁移；若持久化笔记引用，必须登记 ID 重映射。
- **审计没有“所有新子模块自动登记”的规则**：现行审计只覆盖列明的首批模块，其他设定子模块/本人创作/私有资源不自动采集，见 `.trellis/spec/backend/admin-audit.md:7`、`.trellis/spec/backend/rbac-quotas.md:77`。因此最小笔记 CRUD 可沿人工资料既有边界，不必擅自新增审计范围。若规划明确要记管理员修改笔记，再添加 AuditAction/AuditTargetType、前端类型/标签、同事务 AuditOperation 与覆盖测试；不能把笔记正文写入审计摘要。权限校验无论采不采审计均必须执行。
- **整库备份与撤销**：整库备份按数据库保存原表，新表必须通过迁移/恢复兼容测试；新增项目备份 TableRule 不自动意味着智能体可操作笔记，effects 还有独立表白名单。首版若没有笔记 AI 工具，无需顺带新增工具或通用撤销支持。

### 9. 四类来源使用文本 hash + 词典指纹懒校准：可行，但需完整扫描与并发契约

**结论（设计建议）**：可以不在每个既有写入口挂更新钩子，通过读取当前权威原文、计算来源 hash 与词典指纹，在读时校准持久化派生索引。该思路能覆盖生成、撤销、导入、别名修改等遗漏风险；但它节省的是重复匹配和位置计算，不能凭空消除发现变化所需的原文读取/hash 成本。

可复用证据：`analysis_service.content_hash` 对正文原文 SHA-256；四类源都有稳定数据库身份或即将新增的 note ID；当前章节没有每次变化唯一的 revision（第 4 节），因此仅看 ChapterVersion ID、updated_at 或统计表 count/max(id) 均不能严格替代原文 hash。已有 retrieval 缓存签名只有在每条写入都递增 version 时才可靠，见 `backend/app/services/retrieval_service.py:430` 和 `.trellis/spec/backend/retrieval.md`，不能无条件照搬。

建议的最小正确性条件：

1. 先验证作品生命周期和 actor 的来源权限，再枚举允许的当前来源；键至少有 `(project_id, source_kind, source_id)`。聊天枚举显式 JOIN 当前 actor 的会话，不依赖持久缓存曾经授权。
2. 词典指纹应由参与匹配的实体类型/ID、名称、别名、分类及 matcher_version 稳定序列化得到；采用语义一致的去重与排序。同名不同实体 ID 不能在指纹中合并丢失。匹配选项如大小写/排除词若以后接入，也必须进入指纹。
3. 每个来源缓存保存实际参与匹配的原文 hash 和词典指纹；只有二者都一致才能复用。未命中的来源也保存“已扫描且零结果”的状态，避免反复扫描和把未扫描当作零命中。
4. 全书统计必须先确认请求范围内全部当前来源都已经校准，或明确返回构建中/不完整状态；不能只刷新第一页就返回“全量总数”。删除来源通过权威来源集合对账清理，读取命中时仍核实来源存在；删除实体/修改分类通过词典指纹触发重算。
5. 重算与替换某一来源的匹配行采用单个原子事务，并对同一来源的并行重建做互斥或 CAS。原文在扫描后变化时不能让旧计算覆盖已校准的新索引：提交前再核验原文/词典指纹或在相应锁/快照下完成；冲突时有限重试/返回待更新，而不是悄悄标成新鲜。
6. 章节 title/chapter_no 等展示/排序元数据可查询时从当前 Chapter JOIN，避免正文 hash 未变却保留旧章号；若冗余持久化则把相关元数据纳入独立指纹或显式跟随章节重排。
7. 读时维护派生缓存不改变业务原文，不触发 AI、不绑定用户模型；使用独立受控会话或严格事务边界，不能把调用方未提交的 ORM 业务对象顺便 autoflush/commit。即使派生写允许由只读管理员触发，查询结果权限仍按来源约束。
8. 保留源文本与点击目标的一致性验证。即使缓存刚校准，点击前仍可能发生改稿；导航时按当前原文 hash/片段校验，避免旧 offset 落到新文本。

性能限制必须进入后续验证：对知识库 200 × 300 万字的理论上限，每次读取全文做 hash 仍可能过重。建议先按来源/章节范围限定、分批读取并有界 CPU 处理，记录真实基准；若目标性能仍不满足，再引入统一持久 revision 或写入钩子优化，不能在没有证据时保证“懒索引让查询 O(1)”。

## Files Found

- `backend/app/models/entities.py`、`backend/app/models/agent.py`：来源、实体及当前可用身份。
- `backend/app/services/project_service.py`、`chapter_service.py`、`agent/service.py`、`kb_service.py`：权限、生命周期与主要 CRUD。
- `backend/app/services/version_service.py`、`rewrite_service.py`、`analysis_service.py`：快照合并、码点范围、内容 hash。
- `backend/app/services/agent/tools/write_text.py`、`agent/effects.py`：SQL 直接更新与撤销。
- `backend/app/services/story_service.py`、`setting_progression_service.py`、`setting_progression_resolver.py`：全书元数据和按章状态边界。
- `backend/app/services/project_transfer/registry.py`、`importer.py`、`backend/app/schemas/card.py`：备份及卡片兼容。
- `backend/app/services/book_import/service.py`、`splitter.py`、`kb_parser.py`：批量入口、文本规模和分块重叠。

## Related Specs

- `.trellis/spec/backend/admin-projects.md`：人工可管理内容和严格私有资源的边界。
- `.trellis/spec/backend/recycle-bin.md`：作品生命周期与写事务锁。
- `.trellis/spec/backend/retrieval.md`：原文、分块、检索数据和派生缓存。
- `.trellis/spec/backend/database-guidelines.md`：迁移、JSON 与备份注册门禁。
- `.trellis/spec/backend/project-transfer.md`：导入默认值、派生表和撤销共享规则。
- `.trellis/spec/backend/setting-progressions.md`：元数据、剧情字段与 revision。
- `.trellis/spec/backend/story-entities.md`：角色别名匹配与卡片。
- `.trellis/spec/backend/agent.md`：工具消息、效果快照与撤销。
- `.trellis/spec/backend/outline.md`：若持久化章号冗余，必须登记 CHAPTER_NO_REFS 并跟随章节重排。

## External References

本轮只核实项目源码和已存在规范，没有调用外部文档。未提出新框架、第三方依赖或未经验证的库 API。

## Caveats / Not Found

- 没有独立作品自由笔记；知识库也没有在线创建/编辑正文能力。若把知识库纳入本轮，应明确称为“资料”，不能宣称已满足自由笔记。
- 四类来源已确认纳入；尚未决定聊天是否仅计 user/assistant、是否含灵感会话，以及同名/共用别名的归属、正文提及与真实出场的统计口径；这些需要在 PRD 收敛。
- 尚无全书持久提及位置索引；现有 cast_service 只返回角色集合，不能提供可点击区间或出现次数。
- 位置有效性、Unicode 转换、知识库重叠去重、源删除、改名后的全局失效，以及管理员私有聊天隔离应成为后续验收项。
- 本轮未跑测试或性能基准，未修改任何产品文件；本文不是已批准的技术设计。新增笔记及懒校准方案已按主会话追问补充，后续实施仍需要完整契约和定向验证。
