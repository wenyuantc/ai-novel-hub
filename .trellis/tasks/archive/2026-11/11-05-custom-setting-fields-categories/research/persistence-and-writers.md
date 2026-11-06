# Research: 类型化设定字段、分类的持久化兼容与写入口

- Query: 四类可复用字段全部按章回溯时，最小持久模型、全部写入口、项目备份、卡片、智能体撤销、删除与整库备份需要怎样保持旧资料和引用完整性？
- Scope: internal；只研究后端持久化与调用入口，不重复时间线求值核心或前端研究。
- Date: 2026-11-05
- 状态: 规划证据与候选约束，不是已实施方案。主会话已建立顺序子任务 `11-05-typed-setting-fields` → `11-05-setting-auto-categories`；父任务仍为 planning。

## Findings

### 1. 已核实的版本与最清楚的升级安排

| 层面 | 当前源码事实 | 字段子任务建议 | 分类子任务建议 |
| --- | --- | --- | --- |
| Alembic | 唯一仓库 head `0034`，父 `0033` | `0035`，父 `0034` | `0036`，父 `0035` |
| 作品备份 | `ainovel.project` v3，`APP_SCHEMA="0034"` | project v4，`APP_SCHEMA="0035"` | project v5，`APP_SCHEMA="0036"` |
| 卡片 | `ainovel.cards` v3，接受 v1/v2/v3 | cards v4，接受 v1–v4 | cards v5，保存条目标签；作品分类配置不放入卡片 |
| 智能体效果 | 每个 effect `format_version: 2`，验证器仅接受 1/2 | 为新增必需引用图定义新效果版本，例如 3 | 如标签/规则成为恢复必需内容，再升独立效果版本，例如 4；不可给旧效果直接盖新版本号 |

证据：`backend/alembic/versions/0034_planning_matrix.py:6`；`backend/app/services/project_transfer/exporter.py:32`；`backend/app/schemas/card.py:11`；`backend/app/services/agent/effects.py:259`、`:336`。

实际只读命令：在 `backend/` 执行 `PYTHONDONTWRITEBYTECODE=1 LC_ALL=en_US.UTF-8 ./.venv/bin/alembic heads`，结果 `0034 (head)`。这是仓库迁移图，不代表已连接某个运行数据库检查 `alembic current`。

即使最终仅一次功能发布，仍推荐上述单调编号：`importer.migrate` 保留 `1→2→3→4→5`，v4 导入到最终版本时只补分类空态，不重新解释 v4 字段。不要让第二子任务给已经冻结的 project v4 增加“缺失即损坏”的必需表。若坚持最终只有 v4，必须在第一子任务开始前冻结含分类的完整 v4 格式并实现其全部空表/校验，反而模糊两个交付的边界。当前逐级迁移表位于 `backend/app/services/project_transfer/importer.py:280`，旧版本夹带未定义新语义的清理先例见 `:263`、`:268`。

### 2. 找到的文件与职责

| 文件 | 说明 |
| --- | --- |
| `backend/app/models/progressions.py` | 时间线主体、条目字段稳定 ID、JSON 事件、CAS revision、章锚与来源外键。 |
| `backend/app/models/entities.py` | 角色/世界观/组织/职业四张实体表；均为 project CASCADE，组织/职业定义与挂接是不同概念。 |
| `backend/app/schemas/progression.py` | 人工时间线与字段接口形状、五种旧主体、严格数据库整数范围。 |
| `backend/app/services/setting_progression_service.py` | 初始字段写入、旧字段创建/改名/删除保护；也是新写入口应复用的事务边界。 |
| `backend/app/services/project_transfer/registry.py` | 项目表、FK、JSON/字符串内 ID 的唯一登记表，同时决定撤销恢复拓扑。 |
| `backend/app/services/project_transfer/exporter.py` | 备份版本/迁移版本、核心表选择、独立会话流式读取与生命周期复核。 |
| `backend/app/services/project_transfer/importer.py` | 旧备份升级、预览语义校验、重映射、按顺序插入、同事务物化与回滚。 |
| `backend/app/services/project_transfer/remap.py` | 含 ID 的 JSON 与字符串语义重映射；当前未认识新选项/定义/引用句柄 ID。 |
| `backend/app/schemas/card.py`、`backend/app/services/card_service.py` | 当前卡片 v3，按名称关联，仅角色文本字段当前快照；组织创建有绕过 `new_org` 的路径。 |
| `backend/app/services/agent/effects.py` | 快照白名单、效果版本、后续引用保护、Core 删除/恢复、恢复后物化。 |
| `backend/app/services/agent/tools/base.py` | 引用先于 schema 校验、SAVEPOINT 预览、临时新 ID 归一与中文字段标签。 |
| `backend/app/services/agent/tools/write.py` | 旧五类主体及文本工具值白名单；下拉/引用字段尚无工具编码。 |
| `backend/app/services/agent/tools/read.py`、`references.py` | 实体读取不附自定义值；引用白名单、嵌套校验与项目归属。 |
| `backend/app/services/agent/service.py` | 整轮先校验效果版本，再恢复任何一项，避免部分撤销。 |
| `backend/app/services/story_service.py` | 角色/世界观常规创建/修改/删除；创建和元数据修改会显式接入原时间线。 |
| `backend/app/services/organization_service.py`、`career_service.py` | 组织/职业定义与 AI 确认保存；当前创建定义未建立定义级时间线。 |
| `backend/app/services/wizard_service.py` | 覆盖前两次保护检查、批量删除并重建角色/世界观/职业。 |
| `backend/app/services/book_import/reverse.py` | 拆书只写空资料；角色/组织/职业走原语，世界观直接构造后补时间线。 |
| `backend/app/services/entity_ai_service.py` | 单体 AI 返回草稿/建议，不在生成任务中直接保存实体。 |
| `backend/app/services/open_api/entities.py`、`backend/app/schemas/open_api.py` | 平台 Key 开放读取的独立白名单，当前不输出自定义字段、标签或时间线。 |
| `backend/app/services/project_lifecycle.py`、`recycle_bin_service.py` | 生命周期写屏障、恢复保留原项目、purge 直接删除项目父行。 |
| `backend/app/services/backups/database.py`、`backups/service.py` | 原生整库快照，不经 project_transfer；schema_revision 从真实库读取。 |

### 3. 最小新增模型的持久化约束

以下是供设计选择的最小形状，表名可调整；时间线具体求值和值编码以同任务类型化研究为准。

1. **可复用字段定义表**：project FK CASCADE、设定类型键、稳定 ID、label、kind、排序、停用状态、revision。类型键应区分作品内配置范围；不能把现有自由文本 `role_type`/`org_type` 直接当可删除的用户分类。字段定义拥有类型；条目实例拥有各自基础值和事件。
2. **下拉选项表**：definition FK、稳定 ID、label、sort、active。只用 label 作值会让改名断裂；删后用相同 label 重建也不是原选项。被基础值、任何保留事件或分类规则使用时不物理删除，采用停用。
3. **稳定引用句柄候选表**：project FK CASCADE、kind、四选一的 `character_id/world_setting_id/organization_id/career_id` FK，目标 FK 用 NO ACTION，唯一约束防止同项目同目标重复句柄。值只存 `reference_handle_id`，不把不同实体的裸整数混用。句柄不另建目标内容，也不引入全类型软删除系统。
4. **沿用条目字段行的稳定身份**：优先保留 `setting_text_fields.id` 及旧 `SettingProgression.custom_field_id` 指向；新增 `definition_id` 和按类型所需的基础载荷列，或做保留原 ID 的明确表迁移。不能丢弃旧行、按名称重建字段再期待旧事件自动对应。原有稳定 ID、每条目 label 唯一和事件 FK 见 `backend/app/models/progressions.py:90`、`:151`。
5. **组织/职业定义级时间线**：扩充主体 CHECK、唯一索引、schema 和创建入口；旧 `subject_kind="career"` 是“角色+职业挂接”，不是职业定义；`membership` 是“角色+组织”，也不是组织定义。需另用例如 `career_entry`/`organization` 的主体，不改变旧 kind 的含义。现有列为 String(16)；如选择 `career_definition`（17 字符）还必须扩列，不可只改 Literal/CHECK。事实见 `backend/app/models/progressions.py:29`、`:66`、`backend/app/schemas/progression.py:56`。
6. **分类子任务**：最小可用“条目标签载荷 + project/type 范围的分类规则表”，匹配结果读取时计算，不存一份易过期的分类成员快照。若标签采用字符串数组，不含 ID，可登记 `json_plain`；若选择中心标签表和关联表，则均为项目核心数据并显式登记。规则里的 definition/option/tag ID 应优先用真实 FK；如使用 JSON 规则树，必须有专用严格校验与 remap，不能标为 `json_plain`。标签是否随章变化及规则布尔语法由分类子任务定义，本研究不替代其产品决定。

**引用句柄的边界**：目标 FK 能挡住遗漏的实体 DELETE 和批量覆盖，但不能自动证明句柄与目标属于同一项目，也不能保护 JSON→句柄这条没有真实 FK 的边。服务仍要校验项目、字段允许的目标类型；删除句柄前仍需检查全部基础值、有效/失效/孤儿事件以及待保留引用。只要历史继续引用，就不删句柄。不要用 SET NULL 消除历史目标。单作品 purge 的整张级联应允许 project 同时删除句柄和四类目标，需 PostgreSQL/SQLite 实测；NO ACTION 与 RESTRICT 不可互换，项目现有同语句级联约束先例见 `.trellis/spec/backend/story-entities.md:46`。

**基础值存储选择**：可以保留旧文本列、为非文本增加类型载荷，也可以统一新增 JSON 基础值；两者都必须指定唯一权威读取来源，不能让两个列自由分叉。JSON 列必须登记，现有 API 的 `base_value: str` 也不能悄悄开始返回整数/对象，需类型化响应或清楚的兼容适配。现有契约见 `backend/app/schemas/progression.py:217`、`:247`。

### 4. 注册表、导入顺序与嵌套 ID：必须一起改的点

`TableRule` 已支持 fill/fk/deferred/json_refs/json_plain，`RULES` 是插入顺序，逆序也是 effects 删除顺序：`backend/app/services/project_transfer/registry.py:91`、`:146`；effects 直接复用它：`backend/app/services/agent/effects.py:39`。

推荐拓扑（相对顺序，不要求移动无关表）：

```text
作品 → 四类实体 → 引用句柄 → 时间线 → 条目字段实例 → 剧情事件
作品 → 字段定义 → 下拉选项 ────────────────┘
作品 → 标签定义（如有）→ 条目标签关联（如有）
字段定义/选项/标签 → 分类规则
```

- 句柄必须位于四类目标后面，位于含句柄 ID 的 JSON 值前面；否则 `_Importer._values` 在映射表尚未建立时会把其 ID 当 missing。当前 JSON 在每行插入前重映射，只有真实 FK 支持 `deferred`：`backend/app/services/project_transfer/importer.py:961`、`:1114`。不要以为给 `json_refs` 标个名称便会自动延迟。
- 定义/选项先于条目字段与事件，分类规则最后（或至少位于其所有依赖之后）。它们均是核心资料，`option=None`，不能随 snapshots/logs 选项关闭。当前核心与可选选择见 `backend/app/services/project_transfer/exporter.py:59`。
- 时间线现有 FK、事件的 `custom_field_id` 与语义 JSON 分别登记于 `backend/app/services/project_transfer/registry.py:325`、`:340`。新定义 FK、句柄 FK、选项 FK/JSON 列不能遗漏。
- 当前 `setting_payload` 仅递归认识固定 `ANALYSIS_ID_KEYS`，不认识 `definition_id/option_id/reference_handle_id`：`backend/app/services/project_transfer/remap.py:30`、`:124`。新增值采用有鉴别含义的键，或由字段定义向 remapper 传入上下文；不能把“任何整数”都当引用，也不能把含 ID 的用户文本误改。不要复用已有 `type_id`（关系类型）或 `target_id`（智能体目前固定当角色）来表示多态新引用。
- 实际重映射、预览缺失统计、导入语义校验、智能体预览归一必须复用相同语义：`backend/app/services/project_transfer/importer.py:452`、`:864`、`:979`；`backend/app/services/agent/tools/base.py:321`。新增 JSON 基础载荷与分类规则也要接入，不能只处理 `SettingProgression.value`。
- 现有普通 JSON 缺引用允许警告置空，但时间线事实已明确拒绝缺失引用，不降级为警告：`backend/app/services/project_transfer/importer.py:432`、`:591`。本任务的选择/引用值、包括失效/孤儿事件，都应遵循后者，不能导入成功后静默变 null。
- 更新 importer `LABELS`、`ENUM_COLUMNS`、核心表必需列表和 `_check_setting_data`：新主体、字段类型、目标类型、definition归属、option属于字段、句柄目标同项目、字段实例属于时间线、规则只引用其 scope 允许的下拉字段都需检查。普通 ORM 列反射校验并不替代这些业务关系：`backend/app/services/project_transfer/importer.py:129`、`:360`、`:432`、`:693`。
- 完整引用闭包包含只被历史引用的停用对象。现有关系类型 scope 已包含基线、identity/presence 事件且不筛 active，是应复用的模式：`backend/app/services/project_transfer/registry.py:166`。选项和句柄也不能只导出当前值用到的行。
- 所有数据插入、回填、时间线物化、分类规则完整性核验在同一外层导入事务内；任何失败全部回滚。现有提交边界见 `backend/app/services/project_transfer/importer.py:1145`、`:1171`。

### 5. 旧数据库 / 旧作品备份的无损转换

- 数据库 `0034→0035` 与 project `v3→v4` 必须分别实现同一转换规则，不能只修复在线数据库而让旧备份丢旧字段。
- 原 `setting_text_fields` 全部按多行文本解释，保留 row ID、timeline_id、base_value、sort、created_at/updated_at；已有 event ID、custom_field_id、active、来源、章锚、baseline_status/revision 原样保留。只增加与可复用定义的关联，不从“最新投影”反建基线，也不重放旧不完整历史。
- 若按 `(project, 设定类型, 原样 label, multiline)` 聚合可复用定义，只合并定义元数据，不合并条目值/排序/事件。大小写、近义词、不同世界观类型不要做模糊归并。旧同名字段是否语义相同无法由源码证明；更保守的替代是保留带来源标记的旧定义，提供后续显式合并。选择需写入子任务设计，不可在迁移中隐含吞掉一个值。
- 不重用删除后重新生成的 ID 来表示原字段；字段改名保持稳定关联。旧字段删除行为已保护有效事件，并在明确删除字段时清理失效引用：`backend/app/services/setting_progression_service.py:1108`、`:1130`。新增字段定义/选项的“停用”不应沿用该硬删路径去删除全作品历史。
- 原 v1 备份仍先生成 `legacy_unverified` 旧基线，v2/v3 保留原事实，再升级；无需也不能从职业/关系日志猜测新历史。实现先例：`backend/app/services/project_transfer/importer.py:221`。
- 新的组织/职业定义时间线需要空自定义值基线；不能改写原 `career` 挂接时间线。迁移应同时覆盖活跃作品与回收站作品，因为软删除保留整张资料图。
- 新格式 `v4→v5` 补空标签/规则表；如果标签为新增列，旧值明确默认为空，不使用项目 `tags` 填充条目标签。旧版本文件夹带 v5 专属表/列按版本转换清理，不能绕过 v5 的校验。
- SQLite 若需重建 `setting_timelines/setting_text_fields` 父表来调整 CHECK/FK/NOT NULL，须防止外键开启下重建级联删子行。已有事件、引用、source FK、索引和原 ID 必须逐项核验；测试必须有真实旧子行并检查 `PRAGMA foreign_key_check`，不能只在空库执行升级。迁移完整性测试先例：`backend/tests/test_migrations.py:17`、`:170`、`:297`。
- downgrade 应拒绝存在旧版无法表示的新类型值、字段定义配置、引用句柄/规则或新主体历史的情况；仅空态可降级。不能声称“保留当前投影”就是无损。已有拒绝有损降级的模式：`backend/alembic/versions/0034_planning_matrix.py:112`。

### 6. 所有已找到的写入口与接入义务

| 入口 | 当前源码位置 | 本任务必须接入 |
| --- | --- | --- |
| 人工角色/世界观 CRUD | `backend/app/services/story_service.py:101`、`:125`、`:165` | 创建时间线、类型作用域、自定义值/标签写入、目标删除保护；世界观 category 改变不得静默删除旧字段或事件。 |
| 角色单事务创建原语 | `backend/app/services/story_service.py:72` | 新角色获得定义可用性/初始实例；不额外 commit。 |
| 组织 CRUD / 原语 | `backend/app/services/organization_service.py:209`、`:242`、`:250`、`:279` | `new_org` 建组织定义级时间线；字段写入/删目标保护，不只处理成员时间线。 |
| 职业 CRUD / 批量 | `backend/app/services/career_service.py:225`、`:258`、`:265`、`:273`、`:365` | `new_career` 建职业定义级时间线；不要混入挂接状态；删除定义/改类型保留字段事实。 |
| AI 生成结果 | `backend/app/services/entity_ai_service.py:770` | 当前只返回草稿；不应为了字段而变成生成任务直接入库。保存时验证的是当前定义/选项，不信任生成时旧快照。 |
| AI 草稿确认：角色+关系/职业 | `backend/app/services/relationship_service.py:885` | 走 `new_character`，实体/初值/关系/职业全部单事务，引用越权或失效时整体拒绝。 |
| AI 草稿确认：组织+成员 | `backend/app/services/organization_service.py:637` | 走 `new_org`，同上。 |
| AI 职业批量确认 | `backend/app/services/career_service.py:265` | 每个 `new_career` 同样初始化，不产生逐个提交。 |
| 向导准入 + 落库 | `backend/app/services/wizard_service.py:164`、`:184`、`:638` | 两次 `_require_safe_replacement` 都加新字段/历史引用检查。批量 DELETE 即使没自己的值，也可能被其他主体引用，应在消耗 AI 前返回 409，落库前再次核验。 |
| 向导批量替换 | `backend/app/services/wizard_service.py:594`、`:659` | 职业走 `new_career`，角色/世界观直接构造后补 timeline；保留 project/type 配置，但不以空实例使所有覆盖永远被误判有用户内容。 |
| 拆书写入 | `backend/app/services/book_import/reverse.py:828`、`:930`、`:972` | 继续只写空资料/同名跳过；世界观直接构造是独立初始化入口；组织/职业经新原语补齐。若生成不提供新字段，保留为空而非补未来投影。 |
| 卡片角色创建 | `backend/app/services/card_service.py:376`、`:417` | 原字段初始化入口升级为类型化；定义冲突不得悄悄改目标作品配置。 |
| 卡片组织创建 | `backend/app/services/card_service.py:449` | 当前直接构造 Organization，未走 `new_org`；必须补新 timeline/字段/标签初始化，或重用已验证原语。 |
| 卡片职业创建/复用 | `backend/app/services/card_service.py:593` | 新职业原语会初始化；同名复用职业不得覆盖其自定义值/标签；新增引用不能用原同名职业自动匹配规则。 |
| 作品备份导入 | `backend/app/services/project_transfer/importer.py:1073`、`:1145` | 通用 ORM/批量插入不会经过实体原语，必须显式校验新资料图，并在统一事务中物化。 |
| 智能体写工具/预览 | `backend/app/services/agent/tools/write.py:60`、`:233`；`base.py:133` | schema、references、类型化值、事务原语、预览 ID 归一一起升级；批量定义/选项覆盖不应作为可自动执行的小修改。 |
| 智能体撤销 | `backend/app/services/agent/effects.py:492`、`:591` | Core 恢复绕过普通服务，新增表必须进入快照/依赖检查，恢复后验证整个资料图。 |
| 分析回写 | `.trellis/spec/backend/story-entities.md:348`；`.trellis/spec/backend/setting-progressions.md:42` | 当前只生成固定领域变化；如果本任务不增加自动抽取自定义字段，可保留原范围，但共享物化/校验必须认识新字段且不能覆盖手动值。 |

上表基于对 `backend/app` 内 `Character/Organization/Career/WorldSetting/SettingTextField` 构造、主要 DELETE 及通用 factory 写入口的搜索；没有发现另一套直接落库的实体 AI 生成路径。

### 7. 引用目标、字段和选项删除保护

推荐范围内做明确 409，不引入全类型软删除：

- 在 `story_service.delete_owned`、`organization_service.delete_org`、`career_service.delete_career` 中先用统一 helper 检查目标的保留引用，返回可定位到“哪些字段/条目”的 409；不能让新 FK 最终抛成 500。当前它们均直接删实体：`story_service.py:165`、`organization_service.py:279`、`career_service.py:365`。
- 向导批量替换复用同一引用检查，检查“被替换对象被其他主体的字段引用”；当前 `_require_safe_replacement` 只看受影响 timeline 自己的 active progression / text field，遗漏纯入向引用：`backend/app/services/wizard_service.py:184`。
- **全历史集合**应包含基线、所有保留事件（有效、停用、撤销、孤儿），不能只看当前有效值。时间线历史依赖对象删除后无法按章解释；引用句柄 FK 提供底层兜底，业务检查提供清楚文案。
- 当前存在一个句柄但不再有任何保留值引用时，可在项目锁内检查后删除这个无用句柄再删目标。不得用异步“清理未使用句柄”跳过同事务校验；也可以暂不做垃圾清理，保持可预测保护。
- 字段定义停用保留旧实例与事件；有值/事件/规则引用时不硬删。修改类型与缩窄允许目标类型属于资料迁移，最小范围可禁止在已有保留值时修改，并提示新建字段/显式迁移。
- 选项停用后：禁止作为新选择；原基础值/事件可读取、回溯、备份/恢复，显示停用状态。编辑无关字段时不应因原选项已停用而被迫清空；校验需区分“新写入”与“恢复/未修改的旧值”。停用也不能改变分类匹配的稳定选项身份。
- reference handle 的 target FK 建议 NO ACTION；不是 CASCADE（会毁历史），也不是 SET NULL（会改历史意义）。作品 purge 单语句级联必须专测，不可把单条目禁止删除扩大成整部作品无法彻删。

### 8. 智能体 read / write / references / effects 的缺口

**read**：普通 `get_character/list_characters/list_world_settings/list_organizations/list_careers` 通过原 Out schema，不附新自定义值，当前已是如此：`backend/app/services/agent/tools/read.py:82`、`:106`、`:127`。`list_setting_timelines` 仅额外列出 `{id,label}` 字段目录：`backend/app/services/agent/tools/write.py:250`。应提供清楚的定义/选项目录与指定位置有效字段读取能力；按章读取必须调用统一求值，不读取当前实体投影或把列表当前快照伪称历史值。工具输出继续分页/限量。

**write**：当前 `record_setting_progression` 用固定内置字段选择 `text/intimacy/stage_id/identity/presence`，所有 custom_field 默认 text；不支持下拉/引用值：`backend/app/services/agent/tools/write.py:200`、`:277`。新工具载荷用可被 provider 接受的有界类型化对象；根据字段定义核验允许类型、option 归属/状态、handle 目标和章节。基础修正与按章事件共用正式服务，不另写规则。

**references**：当前 `target_id` 固定是 Character；`*_id` 未登记就拒绝，`SettingTextField` 经 timeline join 校验项目；`stages/plans` 才有嵌套 `id` 映射：`backend/app/services/agent/tools/references.py:33`、`:95`、`:105`。需登记 definition/option/handle 及新增嵌套 ID；选项经定义 join，scope 限制仍要业务服务补足。新引用检查要保留“在 schema 丢弃未知字段之前执行”，否则越权 ID 可以藏在暂时没用的 params 里：`backend/app/services/agent/tools/base.py:117`。

**effects**：

- 所有可能被创建/更改/级联删除的新表进入 `EFFECT_TABLES` 和备份 `RULES`，补中文 `_LABELS/FIELD_LABELS`。快照不能只含 field instance，漏掉定义、选项、句柄或分类规则；见 `backend/app/services/agent/effects.py:40`、`backend/app/services/agent/tools/base.py:293`。
- 新句柄的真实 FK 让 `_check_dependents` 自动发现“撤销新实体时后来增加了引用句柄”；不过“撤销新句柄时后来新增 JSON 值”和“撤销新选项时后来新增 JSON 值/规则”仍要专门依赖检查。现有通用逻辑只扫真实 FK，额外 JSON 仅处理细纲与规划角色：`backend/app/services/agent/effects.py:391`。
- `preview_diff` 现在只给 timeline.baseline / progression.value 做现有 ID 语义归一，新实例基础值和规则载荷也要纳入，否则 PostgreSQL 序列增长导致每次确认预览都变：`backend/app/services/agent/tools/base.py:340`。
- `_restore_rows` 有“先解除引用、再逆序删、按拓扑插回、deferred 回填”机制；新引用图需匹配这一顺序。字段改指向时还需保持字段键互斥约束：`backend/app/services/agent/effects.py:518`、`:531`。
- **旧效果兼容是独立版本工作**。当前 v2 删除前快照没有新定义关联/句柄/新主体时间线，不能无条件恢复、靠新列默认值补齐。最小安全策略：新格式递增，对会影响新资料图的旧效果整轮预检 409 `legacy_effect_requires_manual_correction`；可证明不涉及新语义的纯正文/元数据效果继续支持。不能把所有 v2 拒绝当成默认兼容方案，也不能把旧 v2 改写为新版本后继续。若做列默认兼容，需要只接受旧默认且作者未追加资料的状态；已有世界观 aliases 的窄范围先例在 `backend/app/services/agent/effects.py:455`。
- 整轮预检必须早于第一项恢复：`backend/app/services/agent/service.py:569`。撤销后同事务重验句柄、字段、选项和规则，再物化；任一失败全部回滚。现有历史引用保护测试基线：`backend/tests/test_progression_agent_effects.py:189`。

### 9. 卡片：新类型、缺失目标与旧版本的兼容

当前卡片有三个重要边界：所有层级不携带内部 ID；v3 只有角色 `custom_text_fields[{name,value}]`；输出的是当前有效快照，不复制源章节、事件或稳定 ID。证据：`backend/app/schemas/card.py:1`、`:42`、`:107`；`backend/app/services/card_service.py:144`、`:162`；`.trellis/spec/backend/story-entities.md:87`。

建议 cards v4：

- 保持“快照转为新 confirmed 基线”的语义，增加字段定义的可移植类型/选项信息及四类条目值。不要复制源 project_id、definition_id、option_id、handle_id。
- 引用采用**文件局部字符串 ref**，例如 `entity_ref: "e2"`，字段/选项也用局部 key；导入后按本文件映射或作者明确指定的目标 ID 创建本项目句柄。局部 ref 不是源数据库 ID；所有实体先建，再写引用/值，以支持自引用和循环引用。
- 同名字段只有类型及约束兼容时才能建议复用；冲突必须在预览显示并选择复用/改名/放弃本次导入，不能把现有字段配置改成卡片配置。目标项目已有实体选择 skip 时，不覆盖其基础值/历史；当前 skip 自定义字段明确警告的先例见 `backend/app/services/card_service.py:301`、`:391`。
- **不按同名自动绑定新引用目标**。当前旧关系/成员和职业名匹配可保留旧格式行为，但不能用来证明新 typed reference 的身份；角色表本身还允许同名，名称不足以消歧。

缺引用目标的安全选项：

| 方案 | 数据保留与代价 | 建议 |
| --- | --- | --- |
| 预览列出未解决引用，阻止整笔确认，作者显式选择本作品目标或补齐依赖后再导入 | 不写半份、不丢字段；无需新增“失效引用值”产品状态 | **最小推荐**。缺目标时给明确字段/源 ref 清单，不默认清空或 warning 后跳过。 |
| 导出时提示并由作者确认包含引用闭包目标 | 可得到可移植卡片；可能增大文件或加入原未选实体 | 可与上项组合，保持原 2MB 检查；需要扩展当前仅角色/组织选择及职业携带的结构。引用世界观时不能假装现有 CardFile 已有世界观容器。 |
| 导入草稿中保留未解析引用描述，后续手动解决后才落正式字段 | 保留完整意图，但需草稿持久化/界面与新状态契约 | 安全备选，复杂度更高；不能把未解析内容当合法有效 reference。 |

拒绝自动把缺目标转换成文本、null 或偷偷导入一个同名空壳。若 v4 暂不实现世界观目标容器和依赖扩选，那么包含这类外部引用的卡片导出/导入应明确拒绝并保留源资料，作为可见限制交给用户决策，而非假报无损支持。

旧卡片 v1/v2 继续没有自定义字段；v3 仍把 `custom_text_fields` 解释为多行文本快照，不接受旧版本夹带新类型键。已有 before-validator 对 v1/v2 清理 v3 字段，按这个版本闸门扩展：`backend/app/schemas/card.py:116`。`parse_card` 的格式/大小/版本错误仍为中文 400，升级 `CARD_VERSION/SUPPORTED_CARD_VERSIONS` 及测试：`backend/app/services/card_service.py:209`。

主会话已收敛的候选合同是：分类子任务 cards v5 保存**条目标签**，以便标签内容往返不丢；不携带作品的分类规则、显示分组或匹配成员结果。导入卡片到另一作品后按目标作品规则重新归类。包内引用用局部 ref；包外引用在预览中要求作者显式映射，未解决时整笔拒绝，不按名称猜测。

#### 卡片 preview / API / UI 的确切扩展文件

以下仅做卡片合同接点的有界定位，未重复研究前端整体设计：

| 文件及位置 | 需要扩展的合同 |
| --- | --- |
| `backend/app/schemas/card.py:107` | CardFile 的 v4 类型化定义/值、本地实体 refs、外部引用描述；v5 条目 tags；旧版本清理闸门。 |
| `backend/app/schemas/card.py:136` | CardExportRequest 对依赖目标/世界观容器的明确选择或外部描述策略。不能自动包含作者未确认的内容。 |
| `backend/app/schemas/card.py:141`、`:149`、`:154`、`:183` | preview 请求、显式外部引用映射、定义冲突决策、未解决引用列表、可确认状态与结果结构。当前 resolutions 只有角色/组织 skip/rename，不足以表达新绑定。 |
| `backend/app/services/card_service.py:294`、`:376` | preview 根据文件 refs 和作者映射核验，返回带字段/源 ref 的未解决项；import 在项目锁内重新核验当前目标与定义，再保存。不可仅靠客户端禁用按钮保证完整性。 |
| `backend/app/api/v1/cards.py:19` | 当前 preview 只将 `body.data` 传给 service；如 preview 支持映射后复核，须传递完整 schema。import/export 路由继续使用同作品授权。 |
| `frontend/src/api/types.ts:1447`、`:1454`、`:1480`、`:1504` | 扩展镜像类型：本地/外部 ref、显式绑定、定义冲突、预览阻塞项、v5 标签与结果计数。 |
| `frontend/src/api/cards.ts:11`、`:37` | preview/import 传递绑定与定义决策；`defaultResolutions` 不能为引用做名称猜测或默认跳过；旧 skip/rename 行为仅用于旧实体冲突。保留文件大小与 unknown 解析边界。 |
| `frontend/src/components/entities/CardImportModal.tsx:24`、`:43`、`:59`、`:134`、`:197` | 增加绑定状态及预览复核、按 kind 选择本作品条目、逐项展示未解决引用和字段定义冲突；未解决时禁用确认。换文件/作品后清理旧绑定，失败保留当前草稿；服务端仍做最终拒绝。更新当前仅写 v1/v2/v3 的说明。 |
| `frontend/src/components/entities/CardExportModal.tsx:26`、`:37`、`:83` | 明确引用依赖/外部描述，更新类型化字段与标签快照说明；导出结果如果扩展依赖实体，不能仍按原选角色/组织数量报成功。 |
| `frontend/src/pages/project/CharactersPanel.tsx:622`、`:634` | 当前卡片入口与导入后仅 reload/reloadBadges；新字段定义、组织/职业/世界观/标签与分类缓存需在该成功回调或统一数据失效机制内同步刷新。 |
| `frontend/src/api/cards.test.ts`、`backend/tests/test_cards.py` | 扩展版本、请求形状和引用映射断言；导入弹窗需补“未解决不能提交/重新映射可提交/跨项目响应废弃”行为测试。 |

外部引用描述可以展示名称帮助作者选择，但名称仅为提示。最终绑定应是明确的“源外部 ref → 本作品 kind + target ID”，在同一事务验证 ID 有效、归属与字段允许类型。包内 ref 不因卡片重命名而改变身份；包内实体选择 skip 也应在预览明确其最终映射，不借同名候选静默绑定一个 typed reference。

### 10. 开放读取、回收站与整库备份

- **开放 API**：当前 `OpenModel` 与 Character/WorldSetting/Organization schema 是独立白名单，自定义字段不会因为内部 Out schema 扩展而自动出现：`backend/app/schemas/open_api.py:23`、`:98`、`:114`、`:145`。当前 list/get 直接用该白名单序列化：`backend/app/services/open_api/entities.py:17`、`:43`。本任务可以保持该边界，并明确不承诺其支持新字段；若要新增对外读取，需显式新增字段/定义/目标授权投影和测试。不能直接把完整 timeline/baseline/effects 作为开放输出。现有开放设定明确为当前保存值，可能含后续剧情，不能悄悄给它承诺按章回溯，见 `.trellis/spec/backend/open-api.md:77`。
- **生命周期与权限**：新 API/工具/导入后续事务仍用 owner/manageable helper + 项目 active 写锁；仅检查某个 field/handle 的 project_id 不足以处理回收站。集中屏障见 `backend/app/services/project_lifecycle.py:38`、`:50`。人工可管理权限与严格 owner 的 AI/工具边界沿用现有服务，不另创访问捷径。
- **软删除/恢复**：新表均挂 project CASCADE 或其后代，软删不改它们，恢复保留 IDs/事件/规则，不自动重建新值。purge 直接删 Project，必须验证新 NO ACTION 句柄不会阻挡整项目级联，也不会波及其他作品：`backend/app/services/recycle_bin_service.py:212`、`:259`。
- **作品 JSON 备份**：生命周期字段继续排除，导入始终成为正常新项目；新标签/字段不是生命周期状态，必须保存。排除和独立流复核见 `backend/app/services/project_transfer/registry.py:128`、`exporter.py:97`。
- **整库备份**：PG 使用无 table 白名单的原生 pg_dump；SQLite Connection.backup 包含全部表与已提交 WAL，故新增表无需加入 project_transfer 之外的整库表目录。实现见 `backend/app/services/backups/database.py:43`、`:108`。schema_revision 从实际数据库/快照读取，不改成硬编码 0035/0036：`backend/app/services/backups/service.py:495`、`database.py:57`。
- 老整库恢复到隔离库后执行新 Alembic 升级；不要只验证新建空库。新整库回到旧代码不能假装无损支持，沿用拒绝有损 downgrade。当前没有 HTTP 覆盖恢复，本任务无需新建恢复服务。

### 11. 有序验证清单与可复用测试

1. **先验证迁移**：在有旧文本字段、重命名字段、有效/失效/孤儿事件、关系/职业/成员、回收站作品的 0034 库执行 0035；再执行 0036。断言旧 row ID/FK/值/时间戳/章锚不变，新定义归属正确，四类目标同项目；SQLite `foreign_key_check` 和 PostgreSQL NO ACTION/级联均测。空态降级可行，有新语义数据明确拒绝。复用 `backend/tests/test_migrations.py:17` 与 `backend/tests/test_planning_migration.py` 的风格。
2. **注册表反射先过**：复用 `backend/tests/test_project_transfer.py:63`、`:73`、`:95`，验证新表覆盖、每个 FK/JSON 明确处理、插入拓扑。给规则 JSON 的嵌套 ID 增加实际数值断言；反射测试本身看不见 JSON 内的漏映射。
3. **四类字段值 + 历史引用**：重命名字段/选项后仍回溯同一值；停用选项保留历史、拒绝新选择；跨项目 definition/option/handle/目标拒绝；无关编辑不迫使清空原停用值。第 5/20/21 章样例同时覆盖字段与引用，具体求值测试归时间线研究。
4. **每个删除入口**：角色、世界观、组织、职业普通 DELETE；向导准入和运行期间新增入向引用；智能体 DELETE/undo；有效/失效/孤儿事件引用目标；自引用、互相引用；只有无引用句柄时的删除；整部作品 soft-delete→restore→purge。断言是明确 409 或合法整笔删除，绝非 500/历史被置空。
5. **作品格式升级与往返**：旧 v1/v2/v3、字段 v4、最终 v5；每级夹带后版本未知字段/表；定义/选项/句柄/规则缺失或归属错误；仅历史用到停用选项；导入至非空实例验证新旧 ID 不同且嵌套映射正确；失败原计数/资料不变；重导出按行序号比对。已有基线：`backend/tests/test_project_transfer.py:572`、`:636`、`:688`、`:917`。
6. **智能体预览/执行/撤销**：新增定义/选项/引用的预览可重复相等；每层嵌套越权ID被拦；执行不提交外层事务；后来新增 JSON 引用阻止撤销父对象；删除后恢复保留原 IDs/created_at；混合旧/新效果整轮预检不部分改动。复用 `backend/tests/test_agent_tools.py`、`test_agent_effects.py:170`、`test_progression_agent_effects.py:189`、`:330`、`:422`。
7. **卡片**：v1/v2/v3 无损兼容；四类型当前有效快照；局部 refs、自引用/循环、新名称冲突；显式映射本作品目标；缺目标/歧义/跨作品/不兼容定义阻止整笔确认；没有源数据库 ID；失效选项快照可保留；组织绕过原语路径补初始化；2MB边界和中途失败回滚。复用 `backend/tests/test_cards.py:338`、`:439`、`:468`、`:566`、`:642`。cards v5 测标签往返与分类规则不随卡片迁移。
8. **其余创建链路**：手动、with-relations、with-members、职业 batch、三类向导、拆书各自创建后四类字段可用；旧同名跳过不覆盖；AI运行期间定义/选项停用后保存重新校验。复用 `backend/tests/test_entity_tasks.py`、`test_wizard.py`、`test_book_import.py`。
9. **开放读取与生命周期**：白名单若维持原样则断言不意外出现内部字段；若显式扩展则补 Key scope/同项目目标测试；软删期间字段列表、引用查找、导出与写入均拒绝，恢复完整保留。复用 `backend/tests/test_open_api.py`、`test_recycle_bin.py`、`test_recycle_bin_tasks.py`。
10. **整库与最终门禁**：SQLite 持久库快照/恢复后检查新表与历史引用；旧快照恢复再升级；若具备 PG 测试库，验证真实 dump/restore 与 schema_revision。复用 `backend/tests/test_backup_adapters.py`。所有相关切片通过后按后端规范运行 `cd backend && uv run ruff check . && uv run pytest`；研究本身未运行产品测试，也不声称这些测试现已通过。

### 12. 任务切片判断

最初可以在单一任务里按“模型/兼容→时间线/各写入口→编辑→分类→整体验证”执行；但当前用户范围包含两个可独立验收的能力群，主会话已选择两个顺序子任务，这个拆分成立：

- 字段子任务交付四类字段、复用定义、组织/职业定义时间线、所有创建/删除/AI工具/卡片/project v4兼容；不是只交表结构后让编辑或导入落空。
- 分类子任务依赖上述稳定定义/选项/实体身份，交付条目标签、规则自动归类、project v5/cards v5；不再次重写 v4 字段含义。
- 父任务负责跨子任务最终验收：按章值决定下拉分类、标签持久化、停用选项、删除目标、备份/卡片/智能体撤销与回收站完整性。两个子任务无需分别发布，但版本转换分别可测。

不需要按“后端/前端/备份/测试”再建立独立产品子任务：它们属于同一能力的完成义务，单独归档会留下无法端到端验收的半份功能。

## Related Specs

- `.trellis/spec/backend/project-transfer.md`：表与 JSON 登记、版本逐级升级、单事务导入、反射测试。
- `.trellis/spec/backend/setting-progressions.md`：稳定主体、基础值/事件、四类字段未来必须遵循的按章与无未来泄漏约束。
- `.trellis/spec/backend/story-entities.md`：NO ACTION 与项目级联、原语写入、卡片 v3、向导/单体 AI/拆书边界。
- `.trellis/spec/backend/agent.md`：引用先验、服务复用、预览/效果白名单、旧效果与整轮撤销。
- `.trellis/spec/backend/database-guidelines.md`：数据库外键、SQLite/PG兼容迁移、事务/权限。
- `.trellis/spec/backend/recycle-bin.md`：父行屏障、软删全图保留、purge与新入口访问。
- `.trellis/spec/backend/open-api.md`：独立 Key scope、领域白名单与当前值读取语义。
- `.trellis/spec/backend/admin-backups.md`：原生整库快照、真实 schema_revision、隔离恢复。
- `.trellis/spec/backend/book-import.md`：只写空字段、同名跳过、原语与断点同事务。

## External References / Versions

- 本次为内部代码/格式兼容研究，未依赖外部教程，也未新增技术或依赖建议；版本判断以仓库和实际 Alembic heads 输出为准。
- 仓库声明 SQLAlchemy `>=2.0.30`、Alembic `>=1.13`、Pydantic `>=2.7`，这些是范围不是本环境实际安装版本：`backend/pyproject.toml:9`。

## Caveats / Not Found

- 没有连接运行数据库确认其当前迁移版本；只确认仓库唯一 head 是 0034。
- 新表名、字段类型载荷、是否合并旧同名定义、具体世界观类型配置键尚须设计冻结；本文重点是无损与入口约束，不替代时间线/前端研究。
- 引用句柄是候选方案。即使使用真实目标 FK，也仍需要同项目校验、JSON→句柄保护、明确 409 和整项目 purge 测试；不能宣称“加 FK 就覆盖全部路径”。
- 卡片当前没有世界观容器，也没有待解析引用持久状态；新引用不能按旧名称关联逻辑自动绑定。未实现完整依赖携带/显式映射时必须明确拒绝，不静默丢值。
- 分类的标签是否按章、规则何种布尔组合由另一研究/子任务决定；只要规则载荷含 ID，就必须遵守本文登记/重映射/撤销约束。
- 本次只写本研究文件，未修改产品、spec、迁移或其他任务资料，未执行 Git 操作。
