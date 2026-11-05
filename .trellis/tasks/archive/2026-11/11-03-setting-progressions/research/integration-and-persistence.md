# Research: 设定演变的调用链与持久化集成

- Query: 章节 AI 各入口如何读取设定；新时间线与最小文本字段需要同步哪些持久化、撤销、权限及测试契约。
- Scope: internal；仅源码与项目规范研究，不修改产品代码，不运行产品测试。
- Date: 2026-11-03
- 已确认范围：章节级设定演变、追加／替换、最小文本自定义字段、角色状态／人物关系／职业阶段、整章及局部生成／重写。独立场景与完整自定义字段系统不在本期。

## Findings

### 1. 最小一致方案

1. 统一按目标章节解析有效设定，返回独立的只读投影，再交给现有格式化器。不能为了回溯临时修改已加载的 ORM `Character`／`Relationship`／`CharacterCareer`，否则后续 flush 可能将早期状态写回数据库。现有格式化器直接读取对象属性，见 `backend/app/services/context_builder.py:349`、`:396`。
2. 整章生成与 `revise` 共用现有 `build_chapter_context`；自动续写保留同一个 job 的上下文。局部 `rewrite` 五种操作目前不读设定，必须另接一个有预算的设定块，不能宣称只改 `context_builder` 即覆盖所有重写。
3. 章节分析独立读取当前状态、关系及职业，必须消费同一时间线投影，否则分析早期章节仍会把后期设定当作前提。
4. 稳定章节 ID 负责已存在章节的绑定；计划章号是单独的数字锚点。删除关联章节后不按同号重新绑定。详见第 5 节。
5. 新时间线／字段表同时进入项目备份注册表及智能体 `EFFECT_TABLES`；迁移和导入负责重建最新投影。不能只给 HTTP CRUD 接时间线。
6. 除已确认范围之外，组织成员是现有结构化动态状态的真实回溯旁路；旧关系备注也有回退旁路。两者需要在设计中明确处理，见第 4 节。知识库／作者额外要求等自由文本没有时间语义，不应承诺自动消除其中所有后续剧情信息。

### 2. 入口 → 调用链 → 改动与测试矩阵

| 入口 | 当前源码证据 | 本期集成建议 | 关键测试 |
| --- | --- | --- | --- |
| 整章生成 `POST /chapters/{id}/generate` | `api/v1/chapters.py:74` → `services/task_engine/handlers/chapter.py:99` → `generation_service.build_chapter_job`，在 `generation_service.py:279` 调 `build_chapter_context` | 在统一上下文中用目标章节投影替换最新世界观／角色／关系／职业；任务开始执行时读取，沿用当前行为 | `test_context_builder.py:79`、`test_cast_context.py:46`；增加“20 章变化后生成 5 章”的模型实际消息断言 |
| 字数不足自动续写 | `generation_service.py:468`、`:473` 把 `job.messages` 与本轮已生成正文交给 `build_continuation_messages` | 不重新读取最新设定；继承初次生成的目标章节快照，避免同任务中途上下文跳变 | `test_prompt_parity.py:96`；新增未来设定在所有续写轮都不出现 |
| 按分析建议整章重写 `revise` | `generation_service.py:299`、`:304`、`:313` 与 generate 共用 context | 同一投影；保留原文与用户勾选建议，不声称自动删除原文／建议内主动提供的未来信息 | `test_revise.py:28` + 实际模型消息断言 |
| 局部 rewrite / expand / condense / deslop / 光标 continue | `rewrite_service.py:67`、`:99`、`:108` 只取选区、前后文、细纲、风格；`ai/prompts/rewrite.py:23` 无设定参数 | **本期需要新增**目标章有效设定块及预算；仅注入设定，不默认把整章前情／知识库全量塞入局部任务。选区涉及角色应能补充现有章级选角，避免正文有角色但细纲未提及时漏卡 | `test_rewrite.py:21`、`:97`；五种 op 的真实 prompt 都应含旧值、不含后期值；选区预算与 Unicode 偏移原行为保持 |
| 整章 polish | `generation_service.py:326`–`:335` 只传正文、风格、作者要求；`ai/prompts/registry.py:165` 将其定义为保留情节／信息点的润色 | 建议保持正文润色语义；它本来没有读取最新设定的路径。明确其不属于新增设定注入范围，避免无依据增加上下文成本 | `test_prompt_parity.py:57`；确认没有引入当前设定读取 |
| 批量逐章生成／分析 | `batch_service.py:464` 调单章 build；`:524` 调分析 build；每章独立组装 | 自然复用统一解析器，但不能批次开始时把一份旧设定缓存到全部章节；N 章分析后，N+1 章看到生效变化 | `test_batch_generate.py:107` 已验证顺序分析回写进入下一章，应扩展后期变化排除与断点恢复 |
| 手动／自动章节分析 | `analysis_service.py:713` 选角，`:717` 成员，`:721` 职业，`:730` 关系，`:736` 组消息；`_state_card` 在 `:667` 读取当前状态 | 统一解析目标位置的角色、关系、职业；与章末分析生效语义配套。不得再次从最新 ORM 对象拼一次卡 | `test_analysis.py:328`、`:353`、`test_writeback.py`；早期分析 prompt、确认、撤销、重复分析 |
| 编辑器“上下文预览” | `api/v1/memories.py:96` → `context_builder.retrieval_preview`；`context_builder.py:564`–`:580` 仅返回 knowledge/memories；schema 在 `schemas/memory.py:116` | 它目前**不是完整生成提示词预览**。可以增加独立的有效设定预览字段／端点并复用同一解析器；不能在 UI 中把当前两段误称已覆盖设定 | API schema 与 `frontend/src/components/chapter/ContextPreviewPanel.tsx:69` 同步；同章详情预览和生成取值一致 |
| 提示词模板预览 | `prompt_service.py:269` 纯请求模板／示例变量渲染，不访问章节数据库 | 注册新变量及示例值即可；这是模板能力预览，与“实际章节设定预览”区分。旧覆盖兼容见第 3 节 | `test_prompt_templates.py:188`、`:263` |
| 智能体启动章节任务 | `agent/tools/tasks.py:22`–`:45` 白名单；`:102` 使用真实 handler 做预览／校验 | 启动 generate/revise/batch/analyze 后走相同 job，自然使用目标章投影，不新增另一套解析 | `test_agent_tools.py`、`test_agent_runtime.py` + 启动后的 prompt 断言 |
| 普通智能体对话／读工具 | `agent/context.py:301` 只注入作品信息、页面 hint、Skill 和对话历史；`agent/tools/read.py:82`、`:90`、`:118`、`:131` 明确读当前项目设定，参数无目标章节 | 不能宣称普通对话自动按页面章节回溯。最小方案保持其“管理当前设定”语义，若本期增加按章阅读则提供显式 `chapter_id` 或单独读工具，并返回位置元数据；不要仅凭路由把所有工具悄悄改成历史视图 | `test_agent_context.py`、`test_agent_tools.py`，当前管理视图与显式历史读取区分 |

以上表中简写路径除 `frontend/` 外均相对 `backend/app/`；测试路径相对 `backend/tests/`。

### 3. 局部改写的新设定块、覆盖模板与 parity

已核实的现有契约：

- `backend/app/ai/prompts/registry.py:196` 声明 `chapter.rewrite` 的全部变量；`:549` 的 `render` 要求调用方提供每个已声明变量，否则 assert。只改模板、未改 builder 会直接失败。
- `backend/app/ai/templating.py:220` 的保存／导入／预览校验区分普通 required 与 locked。locked 必须顶层原样出现一次，不得在 `if` 内，也不得使用过滤器（`:238`）。
- 普通 required 变量在旧覆盖缺失时没有运行时自动补入；locked 则有兜底：非空代码值未出现在渲染文本里时追加到 user 段（`:289`–`:304`）。
- 预览先 validate；锁定变量值不能由前端请求改写（`prompt_service.py:269`–`:282`）。老版本模板缺新增 required 变量时，恢复版本会返回 400，已有测试固定此行为（`test_prompt_templates.py:263`）。
- `test_prompt_parity.py:114` 对五种局部操作×可选字段做逐字对照；项目对新增可选块已有“为空时原输出逐字不变”的约定，见 `.trellis/spec/backend/prompts-styles.md:58`、`:66`。

建议（待主方案采纳）：

1. `build_rewrite_messages` 增加 `setting_context: str = ""`，调用层传入同一解析器生成并裁剪的文本。不是另写一份关系／职业格式化代码。
2. 如果功能承诺“用户已有覆盖模板也必须收到有效设定”，用一个带位置标题的 **locked 文本块**；单纯新增 optional `T("setting_context", ...)` 会被所有旧覆盖静默遗漏。
3. 默认模板把 `{{ setting_context }}` 放在顶层；标题／分隔空行由代码块本身提供，空值为完全空串，从而保留未传块时的 parity。不要为通过测试删除原对照用例。
4. 现存覆盖运行时由 locked 兜底追加新块，无需覆盖用户模板正文；编辑／导入／版本恢复／预览时必须补齐新锁定变量。这是需要明确展示的兼容变化，不能声称老覆盖所有操作零变化。若要免人工修复，必须另设只追加该占位符的兼容升级流程并保留旧版本，不能靠关闭校验。
5. 增加四个有意义测试：默认非空块正好一次；旧已存覆盖未含变量仍收到块；预览变量锁定且能看到示例；空块仍保持全部五个 op 的旧 parity。真实 job 测试断言 5 章消息无 20 章晋升信息。
6. 新块应包含角色／地点／物品有效字段和必要的关系／职业结果，使用局部任务预算；`prepare_rewrite_job` 当前没有 `context_budget` 参数（`rewrite_service.py:67`），需从路由／设置传入或用现有模型预算帮助函数，不得无界拼接。

### 4. 回溯旁路：需要修复的结构化数据与不承诺自动消毒的人工资料

| 来源 | 证据 | 判断与建议 |
| --- | --- | --- |
| 旧关系备注 `relations_text` | `context_builder.py:385` 在没有结构化关系时回退；`build_cast_section` 在 `:447`–`:454` 以关系列表构造 `related` | 历史解析排除未来关系后，如果拿“过滤后关系列表为空”决定回退，会把旧自由文本关系备注重新注入。应以该角色是否存在结构化管理记录决定是否允许 legacy 回退，不能因本章无有效关系而重新启用；有结构化时间线就由其唯一提供关系事实。 |
| 组织成员职位／状态 | `context_builder.py:433`、`:377` 读取并输出最新 `OrganizationMember`；`analysis_service.py:717` 相同；`writeback_service.py:324`–`:336` 已支持章节分析修改组织成员 | **真实结构化动态旁路**，会把第 20 章“离开／逐出／死亡”带入第 5 章卡。建议最低限度纳入角色状态的成员快照解析；如果本期明确排除组织成员演变，至少不能在历史卡中继续无标记注入已知后期成员状态。是否纳入是主任务应显式收敛的范围决定。 |
| 选角与其他角色名单 | `cast_service.py:111` 接受可选 characters；`:119` 默认查最新角色；`:29` 依 role_type／always_in_context；`context_builder.py:472` 输出其他角色名字与定位 | 如果本期让名字、别名、定位也可演变，投影必须在选角前传入 `characters=`，且 roster 也用同一投影。若这些身份字段仍为全书基础资料，可保持现有选角规则，但不能混用一半历史角色、一半最新卡。 |
| 旧章节正文／分析摘要／剧情记忆 | `context_builder.py:508` 前情只取更早章节；`memory_service.py:382`、`:475` 强制 `< 目标章号` | 保留现有过滤。原文或旧摘要内作者主动预告后续事件不等于结构化设定泄漏，不在本期自动改写范围。 |
| 知识库、作者指令、风格、Skill | `context_builder.py:548` 知识库查询无章号过滤；`generation_service.py:285` 等显式接收作者指令；`rewrite_service.py:100`–`:117` 风格／Skill | 作者提供的全书资料，无可靠故事时间标签。应在 PRD 中明确“按章过滤的是时间线设定，不承诺对所有自由文本识别剧透”；本期不偷偷过滤掉作者指令，也不扩展知识库为时间数据库。 |
| 本卷大纲 | `context_builder.py:330` 选择本卷大纲，并在 `:498` 渲染 | 大纲本来描述规划，允许包含未来情节；不应混淆为有效状态事实，也不能以“上下文不含任意未来信息”为测试目标。 |

### 5. 章节锚点建议：稳定 ID + 计划章号，孤儿不自动复活

现有行为必须考虑：

- 章节号没有唯一约束；插章依 `shift_chapter_numbers` 平移表内所有 `>= from_no` 的登记值（`chapter_numbering.py:82`）。
- 手工 `PATCH chapter_no` 仅同步本章剧情记忆，然后改该章号，不整体平移其他章（`chapter_service.py:209`）。
- 普通删章不把其他章号减一，只清理记忆再删除章节（`chapter_service.py:231`）。
- 细纲整体同步删除空章节时会逐章删除并平移后续号；最后一章保留大纲占号有特殊分支（`plan_service.py:179`–`:200`）。
- 细纲建章先平移，再插入；插入点是否被大纲占用影响平移范围（`plan_service.py:266`–`:294`）。

建议最少的语义字段：

| 字段 | 意义 |
| --- | --- |
| `anchor_kind: chapter | planned` | 锚点语义固定，避免只用 `chapter_id is null` 无法区分计划章和删章孤儿 |
| `chapter_id: FK chapters, nullable, ON DELETE SET NULL` | `anchor_kind=chapter` 时关联稳定章节；排序读取现存 Chapter.chapter_no |
| `chapter_no: nullable int` | 仅 `anchor_kind=planned` 使用且必须 ≥1；chapter 锚点存 null，消除冗余章号同步 |
| 可选 `anchor_label: str` | 创建／重绑定时保存“第 N 章 标题”展示快照；仅为删除后辨识，不参与排序／编号平移 |

- `anchor_kind=chapter && chapter_id=null` **派生为 orphaned**，不必再存一列易漂移的 status；孤儿不参与有效设定计算，UI 允许显式重新绑定或删除。
- planned 以数字生效，进入 `CHAPTER_NO_REFS`。该列只在 planned 行非空，所以现有平移函数无需为 linked／orphaned 加特殊条件。planned 不自动升级成 chapter 锚点；作者明确选择已有章节才绑定 ID。
- 删除关联章后，同号新章、SQLite 重用其他 ID 等不得让旧变化复活。FK 置空保留 anchor_kind 已足够阻断同号复活；不要持久保存裸 deleted ID 后再按 ID 查找。
- 若有 materialized 最新投影，所有插章／直接改号／删章入口须在相同项目写锁、同一事务内更新投影。批量插删最后统一重算，避免每一条平移反复全项目重算；只读解析始终以事实记录排序。
- **兼容旧 chapter_no API**：关系／职业写接口已允许正整数章号及无章号。可适配为：显式新 `chapter_id` 优先；旧章号唯一对应现存章时转换为 chapter 锚点；未建章时保持 planned；重复章号拒绝含糊绑定并要求 ID；无章号遵循已选定的基线编辑规则。不能简单删掉 chapter_no，否则破坏计划未来职业／关系变化的已有能力。旧历史迁移还需区分当年真实关联是否可信，不能只凭现今同号章就推断过去关联。
- 如果选择仅支持已有章节，必须在 PRD 明确旧 API 对未建章号的兼容／降级策略；这不是零代价缩小实现。

### 6. 项目备份、ID 重映射、格式版本

- `project_transfer/registry.py:129` 的 `RULES` 按依赖拓扑排列；每张项目关联表必须入 RULES 或有明确 NOT_EXPORTED 理由。`test_project_transfer.py:60` 反射检查覆盖，`:70` 检查每个 FK/JSON 列，`:92` 检查导入顺序。
- 时间线、基线、文本字段定义／值属于创作数据，应作为核心数据导出，而非放到可选 generation_logs 或 NOT_EXPORTED。
- 新表 FK 指向项目、角色、世界观、关系、职业／阶段、章节、字段、分析来源时逐一登记。若 `before`／`after` JSON 含任一 ID，不能声明 json_plain；使用专门重映射函数。当前 `analysis_payload` 只认识既有 `ANALYSIS_ID_KEYS`（`remap.py:29`、`:106`），新增 progression_id／field_id 等必须扩展或独立 remapper。
- 当前关系／职业历史 `changes` 被明确视为名称 JSON（`registry.py:165`、`:186`）。一旦把它们改成稳定 ID 快照，就需要同时修改 json_plain → json_refs；不能只升级写入格式。
- 当前项目备份 `VERSION=1`、`APP_SCHEMA="0030"`（`exporter.py:32`）。新增核心表按项目 spec 应升备份版本，补 `importer.migrate` 的 v1 → v2（目前步骤表为空，`importer.py:185`）。避免旧应用以同一 v1 接受并静默忽略新时间线表：现有 importer 的未知表处理只是 warning + 丢弃（`:422`）。
- 迁移后旧 v1 导入要构建与本地 DB 升级一致的基线／旧历史状态，而不是“导入成功但回溯丢失”。新 v2 缺失必需时间线信息、非法 anchor 枚举、跨项目逻辑引用须校验。
- 新受限字符串枚举加入 `importer.ENUM_COLUMNS`（`:102`）；新表添加中文 TABLE_LABELS；更新 APP_SCHEMA。读取 schema 失败不能到导入完成之后才被发现。
- 新表涉及循环来源引用时使用 nullable deferred FK 或从低层事实单向链接，避免为方便把所有引用塞 JSON。导入完成后统一重算 materialized 最新值，并保持规定的历史时间戳。

### 7. 卡片导入导出与其他直接写入路径

- 卡片是**无内部 ID、按名字关联的设定快照**，目前 `ainovel.cards` v2、兼容 v1（`schemas/card.py:1`、`:10`）；不包含章节及时间线。
- 导出硬编码当前角色字段、当前职业挂接、当前关系（`card_service.py:135`、`:148`、`:173`），不会自动包含新文本字段。导入硬编码 values 并经 `new_character` 写入（`:381`–`:395`）。
- 最小建议：卡片继续导出某一明确快照（默认最新），导入作为目标作品基线，不复制来源作品章号。为了不静默丢掉本次新增文本自定义字段，卡片可增 v3 的 `custom_text_fields`；维持 v1/v2 导入，字段无值按空处理。若选择卡片暂不支持新增字段，需显式提示并把项目 JSON 备份作为完整保存途径，不可宣传卡片完整往返。
- 世界观目前不在卡片格式中，不要把新世界观时间线强塞进现有角色／组织卡功能；项目 JSON 承担完整搬迁。
- 必查直接创建／覆盖路径：`story_service.py:71` 的 `new_character`、`:99` 的 create、`:115` 的通用 setattr；`wizard_service.py:595` 的角色／世界观整批替换（`:610` 删除、`:611` 直接建 ORM）；`book_import/reverse.py:864` 新角色与 `:928` 新世界观；`project_transfer/importer.py:672` 的 Core 表插入；`agent/effects.py:319` 的 Core 撤销恢复。
- 如果基线保留在实体原字段、时间线仅保存增量，新创建记录可自然从原字段取基线，较少遗漏初始化；如果基线另建表，则上述所有路径必须初始化且禁止懒读写副作用。最新投影与基线不可无约束地双写。
- 向导整批替换删除原实体时，时间线／自定义字段要跟随明确 CASCADE；不能因为没有 SQLAlchemy relationship 就漏清理。现有项目采用数据库 FK 级联。

### 8. 智能体修改与撤销

- 智能体设定 CRUD 经公共业务服务：`agent/tools/write.py:43`、`:216` 将 create/update/delete 委派给 `story_service`、关系及职业服务。因此把时间线写入口接入公共服务可同时覆盖工具修改。
- 工具效果快照并非自动包含全部项目表：`agent/effects.py:39` 的 `EFFECT_TABLES` 是显式白名单，`:68` 只是筛选项目备份 RULES。新表必须同时登记白名单与 label，否则预览差异、确认冲突检查和撤销都会漏数据。
- `effects._restore_rows`（`:319`）通过 Core `insert/update/delete` 按拓扑顺序恢复，不会调用业务服务。只在 `apply_change` 中同步投影不够：效果必须包含原始时间线／基线及最新投影，或撤销结束后显式重算受影响投影。
- `effects._check_dependents`（`:234`）扫描数据库 FK；若时间线用纯 `entity_type + entity_id` 多态整数而没有实际 FK，则依赖扫描与删除保护不会发现它，必须额外补策略。对本期少量实体类型，真实可空 FK + CHECK 通常比无约束多态引用更省集成成本。
- 新事实表纳入 effects 后会扩大“确认前数据变化”冲突范围，这是正确性所需；必须确保 scope 只限本项目，避免把其他作品变动算进 diff。
- 已有 `test_agent_effects.py:295` 断言每个 CHAPTER_NO_REFS 的模型在 EFFECT_TABLES（检索派生表例外）；增加 planned 章号后此测试会揭示遗漏。`:85`、`:170`、`:246`、`:314` 覆盖关联恢复、后加依赖冲突、原子性及删空章链路，可扩展时间线断言。

### 9. 权限、生命周期与事务边界

- 人工设定页面允许有管理权限的操作者维护他人作品：路由 `api/v1/story.py:34`–`:68` 传 `manage=True`；`story_service._write_project`（`:207`）使用 `get_manageable_project(... lock=True, permission="projects.edit")`。
- `project_service.get_manageable_project`（`:39`）进行 read/edit 能力校验、目标保护、活跃生命周期及父行锁；严格 owner 生成与私有资源使用 `get_owned_project`（`:28`）。新时间线／字段 CRUD 应继承人工内容权限；实际 AI 生成仍保持 owner 边界，不能为方便把 AI 入口改成 manageable。
- 每个请求验证目标实体、字段及章节属于同一 project，不仅验证 progression 行的 project_id；路径项目与正文引用项目不一致应拒绝。新 resolver 内部可以接已验证 project，但公开读取必须先走可见性 helper。
- 所有新写操作在父项目写锁内执行，事实变化与最新投影一起提交。恢复／回收站对象不允许旁路写入；软删除保留数据、最终硬删除依 FK 完整清理。
- 底层时间线 writer 只 flush 不 commit，上层 HTTP／分析／智能体单事务调用，避免破坏分析单条保存点或智能体预览回滚。

### 10. 测试与验证计划建议

| 范围 | 应增加的敏感性用例 | 已有测试位置 |
| --- | --- | --- |
| 解析与生成 | 5/20/21 章旧／新状态；乱序插入变化；同章顺序；新增关系前不存在；所有五种局部 op；预览和模型消息一致；解析不污染 ORM | `test_context_builder.py`、`test_cast_context.py`、`test_rewrite.py`、`test_revise.py` |
| 自动分析与批量 | N 章分析后 N+1 生效；重做早期分析不注入后期状态；撤销只撤本事件、不覆盖更晚事实；批量断点恢复重算当前目标 | `test_analysis.py`、`test_writeback.py`、`test_batch_generate.py` |
| 提示词兼容 | 新块正好一次；旧覆盖兜底；锁定变量 preview；缺变量编辑／导入错误可理解；空块 parity | `test_prompt_templates.py`、`test_prompt_parity.py`、`test_skill_generation.py` |
| 章节身份 | 插章／直接改号／普通删章／细纲删章；plan 数字平移；删除后同号重建不复活；跨项目不变；重复章号不含糊绑定 | `test_chapter_numbering.py`、`test_outline_pro.py`、`test_agent_effects.py` |
| 迁移与备份 | 从 0030 有数据升级；NULL/default/backfill；FK 删除；降级保持原有实体最新可读值；v1→新格式；新格式往返及 JSON ID 重映射；缺字段拒绝；故障整体回滚 | `test_migrations.py`、`test_project_transfer.py`、`test_cards.py` |
| 权限与撤销 | owner／他人／管理只读／管理编辑；回收站不可写；删除角色恢复时间线；确认后新变化导致 409；一项冲突整批原子回滚 | `test_admin_projects.py`、`test_rbac_permissions.py`、`test_recycle_bin.py`、`test_agent_effects.py` |

迁移注意：当前最后版本为 `backend/alembic/versions/0030_admin_backups.py`；可规划新 0031，但实现前再次确认迁移头。现有总迁移测试 `test_migrations.py:141` 仅检查一部分 EXPECTED_TABLES 和字段，并非全面 metadata parity，必须为新表写明确的升级／降级／存量数据测试。

验证命令（在相应目录执行；本次研究未执行）：

```bash
# backend/：先针对新逻辑及这些集成边界
uv run pytest tests/test_context_builder.py tests/test_cast_context.py tests/test_rewrite.py tests/test_revise.py tests/test_analysis.py tests/test_writeback.py tests/test_batch_generate.py tests/test_prompt_templates.py tests/test_prompt_parity.py tests/test_chapter_numbering.py tests/test_project_transfer.py tests/test_cards.py tests/test_agent_effects.py tests/test_agent_tools.py tests/test_migrations.py

# backend/：完成实现后的完整门禁
uv run ruff check .
uv run pytest

# frontend/：UI / API 类型接入后的完整门禁
npm run lint
npm run build
npm test
```

项目质量规范明确 macOS Alembic 测试若 `LC_ALL=C` 会以 ASCII 读中文配置；必要时对单次测试命令设置 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`，不能跳过失败或删中文配置（`.trellis/spec/backend/quality-guidelines.md:36`）。

## Files Found / Related Specs

- `.trellis/spec/backend/story-entities.md`：选角／裁剪、关系写入、章节分析回写契约。
- `.trellis/spec/backend/outline.md`：所有章节号引用必须登记；细纲插删章和特殊占号语义。
- `.trellis/spec/backend/project-transfer.md`：核心表导出、JSON ID remap、格式升级、往返测试。
- `.trellis/spec/backend/prompts-styles.md`：模板注册、锁定变量、覆盖／预览／版本、parity。
- `.trellis/spec/backend/batch-generate.md`：逐章生成＋分析及断点恢复。
- `.trellis/spec/backend/agent.md`：工具公共写入口、效果快照与原子撤销。
- `.trellis/spec/backend/rbac-quotas.md`、`admin-projects.md`、`recycle-bin.md`：人工内容可管理范围与 owner-only AI 边界。
- `.trellis/spec/backend/quality-guidelines.md`、`.trellis/spec/frontend/quality-guidelines.md`：测试与完整门禁。

## External References / Versions

未引入新第三方依赖或依赖 API 方案；研究基于当前仓库实现。运行时清单见 `backend/pyproject.toml`（Python 3.12、SQLAlchemy 2、Alembic、FastAPI、pytest），前端门禁命令来自 `frontend/package.json`。Novelcrafter 两篇产品参考已在任务 PRD 记录；它们不决定本项目的章节身份或事务语义。

## Caveats / Not Found

- 具体时间线归并、基线、无章号修改及旧历史可逆性由独立 `timeline-and-legacy` 研究负责；本文件不给出未经确认的产品默认值。
- 旧关系／职业日志不能仅凭名称可靠恢复稳定 ID；本文件只说明新结构的导入映射义务，不将旧日志当成完整快照。
- “所有上下文都没有任何后续剧情词句”无法仅靠设定时间线保证，原因包括作者给出的全文梗概、知识库、Skill、原文、对话历史；验收应限定为受时间线管理的有效状态输出。
- 组织成员状态确有结构化动态旁路，必须显式处理或明确限制；只改角色／关系／职业三个输出函数不能保证角色卡不带后期成员状态。
- 未运行数据库迁移、网络请求或测试；以上验证条目是实现阶段建议，不是已通过证据。
