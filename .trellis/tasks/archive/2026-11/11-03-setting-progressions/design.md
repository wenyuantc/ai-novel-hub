# 设定随剧情演变：技术设计

## 状态、依据与边界

用户已批准本设计并运行 `task.py start`；当前正在分切片实施，完成情况与验证证据以 `implement.md` 和实施记录为准。

用户已确认章节级首版、最小文本自定义字段，以及旧数据采用 M1：保留现有档案、标记待校准，不自动重放旧日志。独立场景、完整字段配置系统不在本期。详细代码依据见 `research/reference-and-boundaries.md`、`timeline-and-legacy.md`、`integration-and-persistence.md`、`ui-and-contracts.md`。

本任务作为一个完整交付保留在同一任务内，按可验证切片实施；数据库、生成与详情维护共享同一时间线契约，不按前后端机械拆成独立产品任务。

## 1. 时间与求值契约

### 1.1 故事位置

- 变化有 `boundary=start|end`：界面分别显示“从本章起”“本章结束后”。手工默认 start，可选择 end；章节分析只能写 end。
- 写作／分析输入使用目标章 start 快照：包含更早章节全部变化及本章 start，排除本章 end。按章查看可切换 start/end。
- 章末必须记录 `(N,end)`，不能存为 `(N+1,start)`，避免插章后源位置错移。
- 同一章边界内先应用自动分析变化，再应用作者明确维护的变化；组内按显式顺序、稳定 id 排序。编辑内容不改变顺序。来源优先只影响同一位置，不让较早人工变化压过较晚分析。
- 基础值 A → 追加 B → 替换 C → 追加 D，结果依次为 A、`A\nB`、C、`C\nD`。替换只作用于目标字段；空串替换清空文本，全空白追加拒绝。结构化字段只支持合法替换。

### 1.2 锚点

使用 `anchor_kind=chapter|planned`、`chapter_id?`、`chapter_no?`：

| 形态 | 规则 |
| --- | --- |
| 已有章节 | kind=chapter，chapter_id 指向本项目章节，chapter_no 存 null；实际排序 JOIN 当前章节号。 |
| 计划章节 | kind=planned，chapter_id=null，chapter_no≥1；保留现有关系／职业的未来章号能力，登记 `CHAPTER_NO_REFS`。 |
| 关联章已删除 | kind=chapter、chapter_id 被 SET NULL；派生为 orphaned，不参与求值，显示保存的 `anchor_label` 并允许重新绑定。 |

计划章不会因同号章节出现自动转为绑定；孤儿变化也不会因同号重建自动复活。旧 API 显式 chapter_no 唯一匹配现存章节时适配为 chapter，没有现存章则 planned；同号多章要求明确 chapter_id。旧审计记录不通过此适配自动迁移。

项目当前允许重复章节号。数值相同的章节处于同一故事序位，按上述边界统一求值；id 只负责身份和稳定排序，不伪造未定义的先后。UI 显示标题区分，编号整理属于现有章节管理能力。

### 1.3 保证范围

统一求值只过滤受时间线管理的设定。作者指令、正文、细纲、全书大纲、知识库、Skill、聊天历史可能主动包含未来情节，本期不自动识别或改写这些自由文本。

## 2. 数据模型与字段范围

新增独立模型模块（拟 `app/models/progressions.py`），不把可维护变化塞入旧展示型审计 JSON。以下名称是实现契约，代码文件拆分可按现有规范调整。

### 2.1 `SettingTimeline`

保存 `id/project_id/subject_kind/revision/baseline/baseline_status` 和真实目标外键。baseline_status 至少有 `confirmed` 与 `legacy_unverified`；新建资料是作者提供的基础设定，旧资料升级为 legacy_unverified。M1主体另有不可逆的 `legacy_compat_allowed` 标记：首次人工基础编辑／确认校准或写入新剧情变化即关闭；值改回原值、删除／撤销新变化也不重新开启。

| 主体 | 稳定身份 | 首版可变化内容 |
| --- | --- | --- |
| character | character_id | profile、appearance、personality、background、current_state（追加／替换）；age、gender、life_status（替换）；条目内自定义文本。 |
| world | world_setting_id | content 及条目内自定义文本；覆盖地点、物品、规则等既有分类。 |
| relationship | 规范化人物对（两个 character FK） | exists、身份组 `{a_id,b_id,type_id}`、intimacy、status、description；身份组原子替换。 |
| career | character_id + career_id | exists、stage_id、note；role 从职业定义校验，生命周期创建包含完整有效阶段。 |
| membership | character_id + organization_id | exists、position、status、note。仅用于防止角色卡中的后期组织状态提前出现。 |

主体外键按类型 CHECK 限定，并建立对应的唯一约束／部分索引；不能使用无真实 FK 的任意 `entity_type+entity_id`。所有引用须属于同一作品，关系类型另允许既有全局预置。关系 identity 的端点集合必须等于稳定主体的人物对，只允许两端交换方向，不能换成同项目第三人；此约束同时用于基础、事件、导入和投影。

不时间化角色名字、别名、定位、always_in_context、世界观标题／分类、组织层级／势力值、职业定义与阶梯排序；它们仍是全书基础资料。组织成员是现有角色状态输出的必要联动，不新增独立组织演变系统。

### 2.2 `SettingTextField`

字段 `id/timeline_id/label/base_value/sort`；仅 character/world 主体可拥有。字段 ID 稳定，改名不改引用，基础值与时间线有效值分开。名称去首尾空白且同条目内不重复。首版只有多行文本，不加入类型模板、下拉、引用、分类或可见性策略。

字段删除默认在仍有有效变化时返回 409，前端提示先删除这些变化；不得静默删除有效剧情记录。已被作者删除／撤销的变化仍受数据库外键保护，因此删除字段时须在同一事务先清理这些不再生效的字段记录，再删除字段；测试覆盖删除变化后删除字段，不留悬空引用或数据库错误。

### 2.3 `SettingProgression`

字段包含 `id/timeline_id/field_key/custom_field_id?/operation/value`、位置、顺序、来源、`source_change_id?`、`source_content_hash?`、revision、active 及撤销／替代状态。custom_field_id 使用真实 FK；builtin 与 custom 引用互斥。独立保存 `protection=automatic|confirmed|manual`：确认／人工编辑提升保护状态但保留原来源ID；同边界自动优先组、替代候选及撤销均使用这一事实，不能以source或revision猜测人工保护。

- value 使用按主体与字段区分的 schema；绝不接受任意 ORM 列。文本值、枚举、数值、关系身份和生命周期快照各有白名单校验。
- 普通事件只保存实际改变的字段；不能把最新完整实体当成早章 patch，以免夹带未来未改字段。
- 关系／职业／成员的创建和解除使用原子存在性变化。创建携带足以恢复主体的完整快照；解除为 exists=false。单次分析变化需要多行字段事件时，全部写入一个保存点，并以同一 source_change_id 关联。
- 类型／阶段的稳定 ID 才是引用事实，展示标签可留快照供旧记录查看。仍被基线或保留的剧情记录引用的类型／阶段禁止删除，返回可操作的 409；定义重命名不是新剧情事件。
- 剧情记录删除／分析撤销停用原事件并重算，不再追加同章反向业务事件。旧审计仍可保留操作证据。

## 3. 单一写入口、求值与当前投影

数据流：人工明确意图／分析结果 → 类型化变化写入口 → 基础值与有效事件 → 只读快照 → 详情预览或 AI 设定块。最新快照同时维护到既有当前表以兼容现有列表。

拟提供三类内部原语：

- `resolve_settings_at(project, position, subjects)`：批量加载并返回独立 DTO；无 commit、无写入副作用，不临时改 ORM 对象做回溯。
- `apply_baseline/apply_progression/revoke_progression`：在既有项目写锁下检查 revision、写事实、校验受影响区间并重算最新投影；只 flush，外层决定事务。
- `materialize_latest`：只负责派生表赋值，不反向调用写事件的高层服务，防止递归或重复历史。

“最新”指全部有效且非孤儿的已记录变化之后的结果，不按最后一次操作时间或已写到哪章推算。查询规模随主体类型批量增长，不能每个角色单独读历史。

关系特殊处理：时间线按稳定人物对存在，关系载体行增加 `is_present`。最新 exists=false 时保留隐藏载体以保住 `RelationshipHistory` 外键审计；list/graph/relations_among 等当前读取默认过滤。区分 `find_current_pair` 与 `find_pair_record`（或同等明确参数）：当前存在性与底层载体唯一性不能共用含糊默认值。普通API、开放API列表／详情／总数、卡片快照、AI缺失关系生成均用current语义；备份、effects、历史和复建使用record语义。复建同对关系显式复用隐藏载体，只有“删除整份关系资料”才清理主体及审计。历史读取从 timeline 枚举，不能依赖当前列表。

职业／成员时间线不依赖可删除的 assignment/member id。最新 exists=false 时可删除当前挂接，历史仍留在稳定主体；后续再次挂接重建当前行。职业主／副上限、阶段归属和关系唯一性在所有受影响故事区间校验，而非只检查最后一帧。修改章节顺序也不能绕过这些约束。职业kind是全书定义，修改时须枚举全部引用该职业的时间线（包括已卸下者），按新kind校验所有区间，再同事务更新定义与投影；历史区间产生两个主职业等冲突时整体409回滚。

## 4. 手工维护与旧 API 兼容

新 UI 明确提供“基础设定”和“剧情变化”，提交时携带对应意图与期望 revision。基础表单加载 baseline，只 PATCH 脏字段，不从最新有效结果整体回填。

旧入口兼容采用以下确定规则：

1. 旧实体 PATCH 先把提交字段与最新当前值规范化比较，相同字段忽略，避免改姓名时把最新状态存回基础值。
2. 真正变化的字段尚无剧情变化时，保持原基础编辑语义。
3. 已有剧情变化的字段，必须显式 `edit_mode=baseline` 或 `edit_mode=progression + position`；缺少意图返回 409 `progression_intent_required`，不猜章节、不改最后一条事件。
4. 显式基础编辑比较 baseline，不能用“等于最新值”规则跳过作者真实的基础修正。
5. 旧 chapter_no 请求按锚点适配规则记录剧情变化；关系表单、职业快捷换阶／卸下、成员维护及智能体工具 schema 同步表达意图。

普通修改无关身份／排序字段不因该角色存在时间线而被拒绝。重大结构化变化使用既有校验；创作上的关系终止／职业卸下与删除误建资料分开。

## 5. 章节分析、确认与撤销

- 分析输入消费章初快照；写回在 `(source chapter,end)` 比较目标位置的值，不拿全局最新值做 before==after 判断。
- 移除“已有更晚分析则跳过早章”的时间线限制：允许补录早章，其后最新状态由故事顺序求值。未解析、跨项目等不合法条目继续走现有 skipped/failed；保留逐条 SAVEPOINT。
- 存活状态、新关系等现有重大变化仍待作者确认。确认时复查章节正文 hash、位置、主体 revision 和目标时点值，冲突返回 409，不覆盖期间的手工编辑。
- 保存分析结果前重新校验正文 hash、源章节身份及该轮输入位置。正文或章序在运行期间变动时，结果可留作过期分析，但不激活它的写回事件。
- 同一源章节、主体、字段的自动分析事件有稳定来源关联：成功重跑该字段时替代自己的旧automatic组；重试同一 source_change_id 不重复落地。先找到旧组、用排除旧组的投影计算和验证新结果，再在同一SAVEPOINT里停用旧组并落新来源。存在旧组时，即使新值等于基础或旧效果，也须完成替代并记录新replace，不能被before==after提前跳过；没有旧组且值无变化才可不新增。失败保留旧组，旧分析不能撤销新组，历史界面显示已替代状态。人工创建／编辑／确认事件不被重跑覆盖，同边界人工优先。
- 新分析未再报告的旧事件保留并标记需复核，不因模型遗漏而静默删除已采用事实；这不扩展为全书修改影响追踪。曾由作者确认或编辑的事件也不被后续自动结果静默替代。
- 撤销根据原事件 ID 与 revision 停用其整组关联事件。原事件被作者改过／替代时 409；无关的更晚事件不阻止撤销，且不得被撤销写回 before 覆盖。例：A→第5章B→第20章C，撤销5章后为 A→第20章C。
- 旧 AnalysisChange 没有新事件关联，属于兼容记录：保留历史查看；仍可执行的旧确认／撤销仅在整个主体legacy_compat_allowed仍为true、无新剧情变化且旧冲突检查通过时，经统一基线写入口按“基础资料修正”执行并保持待校准标记。人工校准A→C→A也不能重新取得旧撤销资格。旧关系／成员创建撤销须检查整个存在性主体，不能直接db.delete绕过隐藏载体与新事件保护。不满足时409，要求在新面板显式修正。

## 6. AI 接入与预览

| 入口 | 契约 |
| --- | --- |
| generate/revise | `build_chapter_context` 在格式化和预算裁剪前使用同一位置快照。 |
| 自动续写 | 使用初次 job 的设定快照，任务中途不重新换入最新状态。 |
| 局部 rewrite/expand/condense/deslop/光标 continue | 新增有预算的 `setting_context`，复用同一求值／格式化；选区或光标附近正文可补充本章选角，不引入另一套名字匹配。 |
| 批量生成 | 每章启动时重新求值；本章分析结果在下一章可见，断点恢复遵循相同规则。 |
| 章节分析 | 章初有效角色、关系、职业及成员，结果按章末写回。 |
| 整章 polish | 保持原本仅润色正文的语义，不新增设定注入。 |
| 智能体 | 发起章节任务自动走同一入口；普通管理读工具仍读取最新设定，不从页面路径暗猜回溯位置。 |

局部提示词将 `setting_context` 作为 locked 块：默认模板顶层放置，空块时保持既有 parity；旧已保存覆盖运行时走现有 locked 兜底，保证非空块恰好一次。再次编辑、导入或恢复旧模板时按当前校验提示补齐新占位符，不关闭模板校验、不覆盖作者模板正文。注册变量、示例、预览与所有 builder 同步。

当前组织职位／成员状态也通过时间线求值。legacy relations_text 是否启用依据“该角色是否已采用结构化关系管理”，不能因目标章尚无有效关系而重新开启旧备注旁路。

新建无需 AI／向量配置的设定预览读取；原 knowledge/memories 预览保持原含义。按条目预览展示结构化有效值；章节预览展示与该章选角同源的设定块并注明它只是设定部分，不冒充完整最终提示词。

## 7. HTTP 与前端契约

以 `/api/v1/projects/{pid}/setting-timelines` 为统一资源前缀：

- `GET` 按类型化目标查询／列出主体（支持已卸下职业、隐藏关系）；`POST` 仅在需要新稳定主体时创建并返回基础快照，读取不懒写。
- `GET/PATCH /{id}/baseline`：基础值、校准状态、revision；PATCH 明确基础修正，确认校准可单独设置，不能仅因保存一个字段就把整份旧基础标为已确认。
- `GET /{id}/progressions?offset=&limit=&field=...`：按故事顺序分页，默认50、上限200，返回 total 与 timeline_revision。
- `POST /{id}/progressions`、`PATCH/DELETE /{id}/progressions/{event_id}`：期望 timeline_revision，类型化位置／字段／模式／值；删除为停用。
- `GET /{id}/effective?chapter_id=...&boundary=start|end`：位置、有效字段、相关事件 ID、校准／孤儿提示和 revision。
- `POST/PATCH/DELETE /{id}/text-fields[/{field_id}]`：稳定字段 ID 和基础值，遵守有效变化引用保护。
- `GET /chapters/{id}/setting-context`：按人工内容读取权限提供本章有效设定，返回 source position 与设定文本／校准提示，不触发模型调用。

复用现有 snake_case、`{"detail":...}` 与可选 machine code；schema/TS 判别联合使用相同类型。422 为类型／枚举／范围错误，400 为非法组合，404 为目标不可见／不存在，403 沿用管理可见不可编辑规则，409 为 stale_revision、progression_intent_required、referenced_field 或状态冲突。提交不做自动重试。

前端复用现有实体 Drawer/Modal，新增 `SettingProgressionsPanel`、`ProgressionEditModal`、`ChapterPositionInput`、`EffectiveSettingPreview`、文本字段编辑区。关系、职业、成员入口复用位置控件；无独立场景 UI、无新全局 store。

基础草稿和变化表单独立保存；变化成功只刷新关联数据，不清空未保存基础输入。请求按用户／权限版本／项目／主体／位置／代次隔离，切换即清空旧结果，409 保留草稿。人工读取／编辑沿用 canReadNow/canEditNow；查看他人作品的管理者不因此获得 AI 权限。刷新同步 characters/world/relationships/careers/organizations/chapters 及新增变化资源。

## 8. 升级、导入导出与生命周期

### 8.1 用户已选择 M1

数据库升级为当前所有角色、世界观、关系、职业挂接、成员建立相同现值的 legacy_unverified 基础快照；旧历史只读保留，不自动解释旧章号、标签、撤销备注或 skipped 建议。升级前后最新档案相同。

待校准基础继续作为默认参考，界面明确“旧资料未校准，不能保证变更记录之前的历史准确”；新的变化与人工校准后的内容按记录确定回溯。不虚构缺失初始值，不自动对全书调用 AI。新建资料各路径统一初始化 confirmed 基础快照，GET 无回填副作用。

旧 API／旧导入产生首次主体时需要明确初始化入口：通用 create、组合创建、向导整批替换、拆书反推、卡片导入、项目备份导入、智能体效果恢复都要覆盖。

### 8.2 章节、权限与事务

章节插入、直接改号、普通删除、细纲同步删章均在现有项目写屏障与事务中更新位置／重算。批量操作末尾统一处理。导致职业约束等冲突时整个操作回滚并说明受影响主体，不能留下事实与当前投影不一致。

新 CRUD 采用人工项目管理 read/edit 权限，引用实体／字段／章锚逐一检查同项目；生成继续 owner-only。软删除作品保留创作数据且禁止新写入，彻底删除按真实 FK 清理。所有底层原语只 flush，兼容 HTTP、分析 SAVEPOINT、智能体预览回滚和最终提交。

### 8.3 备份、卡片、智能体

- 新核心表注册进项目备份 RULES、中文标签、枚举校验和 JSON remapper；baseline/value 中的 type/stage/character 等稳定 ID 均需映射。关系类型导出集合须包含全部保留基线／生命周期快照／事件引用的全局类型（含inactive及孤儿记录），不能只收集当前Relationship.type_id。v2核心引用缺失必须拒绝导入，不能静默置null。备份格式升 v2，v1 导入按 M1 初始化，v2 完整往返保持基础状态、事件顺序、计划／孤儿锚点与自定义字段。实现前再次确认 Alembic head（当前0030，拟0031），同步 APP_SCHEMA。
- 角色卡升 v3，兼容读 v1/v2；导出明确的最新设定快照和自定义文本字段，导入作为目标作品基线，不复制来源章节时间线。世界观完整迁移仍用项目备份，不扩展世界观卡片产品。
- 智能体设定工具采用明确字段对象：`record_setting_progression.value` 恰填 text / intimacy / stage_id / identity / presence 中一个；列表附本页自定义字段 id/label。各模型供应商的工具声明需保留类型和引用校验，Gemini 不能发送无类型或自由对象字段。
- 新事实表同时进入 `agent/effects.py` 的 EFFECT_TABLES、差异标签与依赖检查。新effects记录格式版本，Core撤销恢复必须恢复基线／事件／字段并重算或验证投影，一次冲突整批回滚。升级前没有时间线支持版本的effects，若涉及时间线主体或章节位置／存在性，明确409 `legacy_effect_requires_manual_correction` 并保留旧记录，引导显式修正基础；纯正文等无关旧effects保持原能力。先判版本与影响范围，不能先执行Core恢复再发现基线缺失，也不把M1新增依赖误报为作者新修改。
- `source_change_id → analysis_changes.id ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED`；来源删除不删除变化事实、protection或顺序。PostgreSQL 同一删章可能先后触发章节和分析来源两条级联路径，延迟引用校验可使两列安全置空，非法引用仍在提交时拒绝。v2继续将章节分析／回写条目作为核心数据导出并映射来源，不新增分析导出开关；generation_logs的可选性与本字段无关。来源丢失后禁用原分析按钮撤销，相关变化按独立事件在面板维护；手工已重绑其他章节的事件仍可生效。

### 8.4 规划核对结论

时间线核对的5项与集成核对的4项已纳入上述合同：旧基线校准保护、职业kind区间检查、相同值重跑来源替代、固定关系端点、独立人工保护状态；旧effects闸门、历史关系类型导出闭包、current/record读取分离、分析来源SET NULL。完整证据见两份 `research/design-review-*.md`，实现阶段须有对应敏感用例，不能仅以文档已记录视为通过。

## 9. 风险与回退

- 历史缺口属于 M1 明示限制，不通过将测试期望改成“当前值即可”规避；核心时间旅行用新建／已校准数据验收，另测旧数据提示。
- 最大风险是旧写入口与当前读取旁路：以研究入口矩阵逐项签收，尤其职业快捷操作、成员、旧关系备注、向导和智能体 Core 恢复。
- 不新增框架／数据库／缓存依赖。先实现纯求值和持久化最小闭环，再接分析与 UI；每步保留可执行回归点。
- 迁移上线前留完整备份。downgrade 如支持，先将最新有效值写回旧表，再删除新结构；旧版本无法表达历史和自定义字段，回退会损失这些新增能力，不能称为无损降级。若存在隐藏关系载体，降级明确拒绝，避免复活关系或级联删除审计；需先由作者处理或恢复备份。恢复完整新功能需恢复 v2 备份。
- 本期不部署、不自动改用户实际作品、不自动运行旧书分析；实现及质量检查完成后再按项目流程收尾。
