# Research: 类型化自定义字段的章节时间线

- Query: 在保留旧 `SettingTextField.id` 与既有事件关联的前提下，怎样让作品级复用字段覆盖四类值、组织与职业定义，并让查看及 AI 使用同一章节有效值？
- Scope: mixed；以内部模型、求值、服务、上下文及现有测试为主，外部仅核对 JSON 空值和类型联合文档。不重新调研前端或备份实现。
- Date: 2026-11-05
- Task: `.trellis/tasks/archive/2026-11/11-05-custom-setting-fields-categories`；规划研究，不代表用户已批准实现。

## Findings

### 结论与范围

推荐在原 `setting_text_fields` 上增加共享定义关联，并将基础值升级为 JSON；保留每主体字段行的主键和 `SettingProgression.custom_field_id` 外键。事件依旧使用现有表、章锚、排序、人工保护、revision 与求值原语。新建作品级字段定义、稳定选项和轻量引用句柄三类资料；新增 `organization` 与 `career_entry` 时间线主体，分别代表组织、职业条目自身。公共 EntityRef.kind 仍使用 `career`。

四类值全部回溯：多行文本 `append/replace`，单行文本、单选下拉、单引用只 `replace`。不能用最新值覆盖早期章节，也不能把三类新字段做成静态资料。以下精确结构是技术建议，应在子任务设计中固定后实现。

当前父 PRD 已明确：标签、分类规则、字段名、选项名、条目名属于整理／身份元数据，本期不新增名称时间线；回溯的是字段值和引用身份。引用可显示目标当前名称，但不递归引入目标正文、不自动选角。分类可按当前或指定章节求下拉有效值；本研究不把标签或规则额外时间化。

### 1. 已核实源码事实及文件

| 文件与位置 | 事实及用途 |
| --- | --- |
| `backend/app/models/progressions.py:29`、`:57` | 数据库主体形状与五个部分唯一索引区分角色、世界观、关系、职业挂接、组织成员。 |
| `backend/app/models/progressions.py:66` | `subject_kind` 当前为 `String(16)`；`career_definition` 长 17，采用该名称时必须同时扩宽列。 |
| `backend/app/models/progressions.py:90` | `SettingTextField` 是每条时间线的字段；`id` 稳定，`label/base_value/sort` 都在条目行，基础值为非空 Text。 |
| `backend/app/models/progressions.py:95` | `(timeline_id,label)` 唯一，不能假设共享模板的同名字段可以与旧字段直接共存或合并。 |
| `backend/app/models/progressions.py:113`、`:153`、`:155` | 事件恰选 `field_key/custom_field_id` 之一；自定义字段 FK 指向旧表；事件值已是 JSON，无须为四类值另建事件表。 |
| `backend/app/schemas/progression.py:8`、`:49` | 公共主体 Literal 和主体引用组合也只支持原五类，必须与数据库约束一起扩展。 |
| `backend/app/schemas/progression.py:113`、`:143`、`:206` | 事件值已接受 JsonValue；PATCH 用省略／显式 null 区分；有效值输出仍是 `dict[int,str]`。 |
| `backend/app/services/setting_progression_resolver.py:163`、`:181` | 自定义字段只允许角色／世界观，并无条件使用可追加的文本规则。 |
| `backend/app/services/setting_progression_resolver.py:255`、`:267` | 纯求值负责故事位置、章初章末、人工优先、孤儿与排除事件；`position=None` 表示所有有效事件后的最新值。 |
| `backend/app/services/setting_progression_resolver.py:288`、`:292`、`:319` | 自定义基础值只接受字符串，使用浅复制，事件也强转字符串；对象型值加入后必须改深复制及分类型操作。 |
| `backend/app/services/setting_progression_service.py:91`、`:246` | 原子领取时间线 revision；批量只读求值在 `no_autoflush` 中查询标量，且要求写调用方提前 flush。 |
| `backend/app/services/setting_progression_service.py:352`、`:519` | 物化和人工创建入口均限制主体类型；只扩展 schema 会在这里失败。 |
| `backend/app/services/setting_progression_service.py:734`、`:770`、`:951` | 事件校验先检查字段归属；创建可组合事务，更新合并既有值后重新校验；分析事件不能更换字段身份。 |
| `backend/app/services/setting_progression_service.py:844` | 事件 DTO 直接读 ORM 的 `value`；采用内部 handle 后，输出边界需显式解码。 |
| `backend/app/services/setting_progression_service.py:1050`、`:1090`、`:1130` | 字段 CRUD 按单时间线归属／版本校验；有 active 事件时禁止删除，旧删除逻辑会清理 inactive 事件。 |
| `backend/app/models/entities.py:269`、`:390`、`:426` | Organization、Career 定义与 CharacterCareer 挂接是不同业务对象。 |
| `backend/app/services/career_progression_service.py:27` | `career` 主体稳定身份是角色＋职业，基线包括 `exists/role/stage_id/note`，并非职业定义。 |
| `backend/app/services/career_progression_service.py:332` | 已有 `protected_stage_ids` 扫描基线及所有保留事件，包含停用和孤儿；可复用其“历史引用也需保护”的模式。 |
| `backend/app/services/context_builder.py:470`、`:490`、`:519` | `_custom_labels` 假定值能 `.strip()`；角色／世界观均按目标章初求值，再格式化，不能绕开这条路径。 |
| `backend/app/services/context_builder.py:575`、`:601`、`:668` | 历史成员／职业挂接决定相关组织／职业；组织与职业定义仍从当前行读取。 |
| `backend/app/services/planning_context_service.py:334`、`:347`、`:389` | 未建章只显示身份并警告；已建章角色／世界观按章初解析，但显式组织／职业目前输出静态资料。 |
| `backend/app/services/analysis_service.py:668`、`:724` | 分析已复用有效角色及自定义字段；组织块只输出名称，职业块输出阶梯，不能假设新增组织／职业字段会自动进入分析。 |
| `backend/app/services/story_service.py:125`、`:155` | 世界观 category 是直接修改的元数据；仅改 category 时，`apply_world_content_update:618` 可直接返回，不领取 timeline revision。 |
| `backend/app/services/organization_service.py:209`、`backend/app/services/career_service.py:225` | 创建定义条目只有原有实体初始化，尚无定义自身的时间线／共享字段绑定。 |
| `backend/app/services/wizard_service.py:184` | 向导覆盖通过主体 FK 查 active 事件或任意自定义字段防止级联破坏；新主体和 incoming 引用需要纳入。 |
| `backend/app/services/agent/tools/write.py:200`、`:277` | 智能体工具将自定义值固定解释为 text，且拒绝 null；即使 HTTP 支持，工具不改也无法正确写下拉／引用／清空。 |
| `backend/app/services/agent/effects.py:40`、`:518` | 效果快照表白名单、恢复次序和互斥字段恢复是显式规则；新定义／选项／句柄必须覆盖，JSON 引用不会自动被 FK 遍历发现。 |
| `backend/app/schemas/planning.py:28` | 已有 `{kind:character\|world\|organization\|career,id}` 的 EntityReference 形状，可复用公共契约，不必把句柄暴露给用户。 |

### 2. 演进方案比较

| 方案 | 兼容与复杂度 | 判断 |
| --- | --- | --- |
| A：旧字段表增加定义关联、基础值 JSON 化，旧端点适配 | 所有旧 field ID、事件 FK、来源唯一索引、过滤语法和撤销关联保留；只有一套绑定／求值。需要审慎迁移 Text→JSON 和旧 label 唯一约束。 | **推荐**。迁移有明确边界，运行期最少分支。 |
| B：新建值表，旧文本表与新字段并存，事件增加第二种字段 FK | 可避免立即改旧基础列；但事件必须从“两选一”扩展为“三选一”，求值、过滤、API、来源唯一索引、撤销和导入都长期双轨；旧 field ID 和新 value ID 还需命名空间。 | 不推荐作为最小方案。兼容分支比 A 多，难以证明消费链不漏一种。 |
| C：迁移到统一新表，同时重写全部旧 ID／事件关联 | 运行期可统一，但需映射所有旧 id，且影响来源、历史、快照及既有客户端；若保留原 id 可降低风险，但仍需更换 FK／表规则。 | 无足够收益，不作为本期首选。 |
| D：把全部字段值塞进 `SettingTimeline.baseline` JSON | 少建一张值表，但混入固定字段；丢掉当前字段 FK 与稳定 id 模型，需要再造绑定身份。 | 不满足最小安全与兼容目标。 |

不推荐序列化成字符串假装兼容，例如把引用保存成 `"{...}"`、选项保存显示名，或把新对象 `str(value)` 注入 AI；这些都丢失可验证身份。

### 3. 定义、每主体绑定与旧 ID

建议形成三个清楚的身份层次：

```text
SettingFieldDefinition（作品内复用配置）
  id / project_id / field_type / label / applicable_types / sort / revision / archived
  ↓ definition_id
SettingTextField（保留旧表名及主键；语义扩展为主体字段绑定）
  id / timeline_id / definition_id? / base_value(JSON) / legacy label、sort
  ↑ SettingProgression.custom_field_id（不变）
SettingProgression.value(JSON)（不变表，改类型校验）
```

- `definition_id=NULL` 表示原条目级多行字段；不回填猜测的共享定义，不按 label 自动合并。旧基础文本逐字保存为 JSON 字符串；已有事件字符串无需 wrap、重写或重新分配 ID。
- 新共享绑定唯一键为 `(timeline_id,definition_id)`；定义负责唯一权威的类型、名称、适用范围和排序，绑定负责该主体的故事起始值。每个主体保留自己的事件序列，共用定义不等于共用值。
- **旧 label 约束不能忽略**：推荐共享行 `label=NULL`，名称从定义 JOIN；legacy 行保留 label。保留 `(timeline_id,label)` 唯一键，并把 label 非空 CHECK 改成“legacy 必须有合法 label／共享行无本地 label”，再增绑定唯一键。这样同名 legacy 与新定义可并存，显示层应能区分来源。另一可行做法是改为仅 legacy 的部分唯一索引，但仍不得维护两份可独立改名的名称。
- 显式将旧字段关联到复用配置时，设置其 `definition_id` 并将本地 label 置 null、名称权威移交定义；保留 `id`、基础值、所有事件及来源，只允许兼容的多行定义。若该主体已有同定义绑定，默认 409；只有空绑定且无任何事件时，才可在同事务删空绑定并接入旧行。两行都有资料不能自动合并。
- 不建议在本期开放有数据字段的类型原地转换。类型改变会让旧 append 事件或引用形状失效；需要新建定义，后续显式迁移。字段改名、选项改名只改元数据 ID 不变。
- 推荐共享空字段通过定义 LEFT JOIN 现有绑定只读展示，`field_id` 可以为 null；首次真正写值／事件时，在项目写锁下创建绑定。空起始值为文本 `""`、下拉／引用 `null`，模板配置本身不写入任何主体的非空起始值。有效快照 map 只按已有 binding ID 建键；空槽用 definition_id 识别，不能混用两个 ID。
- 首次记录事件也可能尚无绑定。新入口须接受“主体＋definition_id”并在同事务完成绑定和事件，或显式 ensure-binding 后返回稳定 ID；不能要求调用方提交不存在的 custom_field_id。旧事件路由仍只接已有 binding ID。多步 UI 若选择先 ensure、再写事件，需允许产生安全的空绑定并处理版本变化。
- 批量预建空绑定也是可行备选，但会放大目录变更写入量，且旧向导把任意自定义行视为历史保护。采用惰性空槽时，无论字段求值、分类、预览还是条目列表，都不能为了显示空字段创建 binding／timeline；写原语负责幂等初始化，唯一约束与目录版本防竞争。
- 每主体值／事件沿用 timeline revision；定义操作另有 definition/config revision。只有项目锁仍不足以识别页面过期配置。写值时校验其读取的定义版本，防模板变更与旧草稿竞争；定义元数据不冒充某个主体的 timeline revision。

### 4. 新主体与现有授职／成员时间线分离

| 时间线主体 | 身份 | 本期负责 |
| --- | --- | --- |
| `career`（保留） | `character_id + career_id` | 角色是否挂职、阶段、备注、主副职业历史约束。 |
| `membership`（保留） | `character_id + organization_id` | 角色成员存在性、职位、状态、备注。 |
| `career_entry`（新增） | 只有 `career_id` | 职业条目的四类自定义字段。 |
| `organization`（新增） | 只有 `organization_id` | 组织条目的四类自定义字段。 |

新增两种 CHECK 分支和各自部分唯一索引；`TimelineCreate.validate_identity`、`TimelineOut`、API query Literal 同步。冻结建议名为 `career_entry`，兼容当前 String(16)；若另改为 `career_definition`，必须连同列宽一起迁移，不能只改 Literal。不要把公共 EntityRef.kind 从 `career` 改成时间线内部主体名。

新增主体先只支持自定义字段，固定字段规则集合为空，`baseline={}`；不放入 `_LIFECYCLE_FIELDS`，不参加角色职业数量验证，也不调用职业挂接的物化函数。`_materialize_many` 须认识新主体、验证其自定义事件并允许无固定字段投影。组织宗旨、职业阶梯等既有固定资料不因此宣称获得历史版本。

写入时初始化定义时间线可以复用角色／世界观幂等原语的模式；新组织、职业的 `new_org/new_career` 及对应批量／AI入口应走统一初始化。既有组织／职业新增的空自定义基线不需要把静态档案冒充旧剧情快照。

### 5. 四类值的精确建议合同

定义元数据判别字段类型，基础值与事件值使用相同 canonical JSON 形状。客户端不能用自报类型绕过定义校验。

| field_type（建议名） | canonical 基础／事件值 | 公共输入／输出 | 清空及操作 |
| --- | --- | --- | --- |
| `multiline` | `str` | 原字符串 | `replace ""` 清空；`append` 只接受 `value.strip()` 非空的字符串，按现有换行拼接。 |
| `singleline` | `str` | 原字符串 | 只 `replace`；`""` 清空；拒绝换行，不把换行自动压平。 |
| `select` | `{"option_id": PositiveDbInt}` 或 JSON `null` | 同左 | 只 `replace`；`null` 清空；`""/0/[]` 不是空值。 |
| `reference` | `{"reference_handle_id": PositiveDbInt}` 或 JSON `null` | `{kind:character\|world\|organization\|career,id:PositiveDbInt}` 或 `null` | 只 `replace`；服务在公共 EntityRef 和内部句柄间转换，用户界面不讲 handle。 |

- 对象必须恰含约定键；ID 为严格正 int32，拒绝 bool、数字字符串、越界数、额外键、跨定义选项、跨作品引用。单行建议至少拒绝 `\r/\n` 及 Unicode 行分隔符；长度上限若需要应由设计统一规定，不能擅自套入某个旧字段的长度。
- 不自动 trim 旧多行内容或把空白替换成 null；显示可过滤空白，存储应保留文本。四类空值不使用同一种 truthiness 判断，显式 `null` 不等于“本次未修改”。
- 事件 CREATE 的 `value` 必须出现；PATCH 省略 `value` 保留原值、显式 `value:null` 清空下拉／引用；对文本的 null 是校验错误。新增基础值 PATCH 也必须使用 `exclude_unset`，不能用 `exclude_none` 吞掉清空意图。
- SQLAlchemy JSON 明确使用 `none_as_null=False` 表达 JSON null，保留数据库 `nullable=False`；SQL NULL 和 JSON null 不混用。默认值要明确，不把 `default=None` 当作 JSON null 默认。参见下文官方资料。
- 纯 resolver 接收字段规则映射（按 binding id，包含类型）以及 typed 基础值。形状校验只写一处，服务负责归属／可选择性，resolver 负责相同形状与操作规则。`custom` 基础映射及写入对象均深复制；当前浅复制只因值为字符串才安全。
- 保留现有章锚和排序，不为下拉／引用再造时间轴；`replace null` 是普通事件，后来的非空值仍正常覆盖，撤销清空也按已有排除事件机制计算。

**读历史与创建新选择需区分**：退役选项或缩窄的目标类型不能让旧事件无法回放。重放校验身份和形状，新选择校验“当前可选”。仅改事件备注或锚点且 value 未变时，允许保留其旧身份；不能因合并 PATCH 后查到退役选项就禁止维护旧历史。

### 6. 选项与引用的稳定身份

#### 6.1 下拉选项

`SettingFieldOption` 独立保存 `id/definition_id/label/sort/retired`。事件、基础值及分类规则比较 option ID；label 和排序可改而不更换 ID。停用只从新选择列表移除，历史仍能解析。建议普通配置操作不硬删已被使用的选项；首版可统一退役，避免 JSON 中的历史引用悬空。新表采用不可复用的整数身份策略，SQLite 可用 AUTOINCREMENT，PostgreSQL 不回退序列。

停用不是把已有选中值改成 null；分类以对应时点的 option ID 比较，不能以当前 label 或数组下标比较。字段定义归档也不能删除绑定／历史或让 resolver 忽略它们。

旧 `delete_text_field:1148` 会清理 inactive 事件，这个明确的旧条目删除行为不能直接复用为“共享定义删除／选项删除”。共享配置优先归档，历史保护包含全部保留事件；旧适配端点不能借删除单条字段去硬删共享资料。

#### 6.2 轻量引用句柄（推荐）

```text
SettingReferenceHandle
  id / project_id → projects ON DELETE CASCADE
  kind = character | world | organization | career
  character_id?    → characters NO ACTION
  world_setting_id?→ world_settings NO ACTION
  organization_id? → organizations NO ACTION
  career_id?       → careers NO ACTION
  CHECK(kind 与四选一 FK 对应)
  每个 kind 的项目＋实体部分唯一索引
```

同一目标的基础值和事件共用句柄；句柄指向实体本身，引用职业使用 Career 定义，不使用角色挂接时间线或 `CharacterCareer.id`。句柄的创建、项目归属和 allowed-target-types 由公共服务在项目锁下统一验证；对外仍采用现有 EntityRef。

| 方式 | 安全边界 | 判断 |
| --- | --- | --- |
| JSON 只保存 `{kind,id}`，所有删除入口扫描 | 少一张表，但 DB 不知道引用；遗漏旧删除、向导、智能体或批量路径就会悬空，整数复用可能串到另一个条目。 | 不推荐。 |
| 轻量句柄＋目标 NO ACTION FK | 多一个小表；漏掉应用删除检查时数据库仍阻止删除目标，identity 不会改指同号新条目。无需把四类实体改为软删除。 | **推荐**。 |
| 给全部实体增加软删除／墓碑注册系统 | 可保留目标删除后的历史显示，但改变整项目生命周期、查询和还原语义。 | 超出最小方案，不建议本期引入。 |

删除规则建议：

1. 检查所有仍保留的基础值与事件，包括 inactive、孤儿、被替代事件；不能只查当前有效值。已有 `protected_stage_ids:332` 提供这种扫描模式。
2. 源条目被任意保留字段历史引用时，单独删除／向导覆盖返回结构化 409；对重复利用裸 ID 不做任何猜测回退。
3. 句柄不提供普通删除入口。仅在项目锁内证明没有任何保留值／事件使用时，允许内部清理未用句柄，再删目标；无法证明则拒绝，不能级联删句柄让 JSON 自行失效。
4. **DB 双保护的准确含义**：NO ACTION 保护“句柄→目标”，JSON `reference_handle_id` 本身没有数据库 FK；“不能删仍使用的句柄”仍需统一服务和效果恢复检查。不能宣称 JSON 已有数据库引用约束。
5. 作品完整删除通过 project→句柄与 project→实体的同事务级联清空。沿用 NO ACTION 而非立即 RESTRICT；是否需要延迟约束由真实 PG／SQLite级联测试确认。目标 FK 不得设 CASCADE 或 SET NULL。
6. 捕获旧删除路径的 IntegrityError 时应映射为可理解的 409 并回滚，不可让其成为 500。向导须在创建任务及最终落库前检查 incoming 引用，避免运行模型后才失败。

### 7. 公共 API 兼容建议

不能直接把现有 `EffectiveOut.custom_fields: dict[int,str]` 改成对象 map 并仍宣称旧客户端兼容。推荐加完整 typed 字段输出，同时保留旧字段的文本投影：

```json
{
  "custom_fields": {"71": "旧多行文本"},
  "field_values": {
    "71": {"definition_id": null, "field_type": "multiline", "value": "旧多行文本"},
    "72": {"definition_id": 12, "field_type": "singleline", "value": "掌门"},
    "73": {"definition_id": 13, "field_type": "select", "value": {"option_id": 8}},
    "74": {"definition_id": 14, "field_type": "reference", "value": {"kind": "world", "id": 42}}
  }
}
```

- `field_values` 是已有实例的完整 typed map（属性名在子任务冻结）；键仍为 binding ID，每项按 field_type 做 Pydantic 判别联合，值对象不是自由 JsonValue。label／选项名／引用当前显示名可以另带只读元数据，不塞入 canonical value 充当身份。未实例化空槽由字段目录／条目字段查询返回，不临时分配 ID 或写入求值结果。
- 旧 `custom_fields` 保持字符串，建议投影全部多行字段；对应旧 `/text-fields` 列表也只返回多行，并通过公共元数据解析 label。若只保留 legacy 多行投影也可，但两处范围必须一致且写入设计，不可一个包含共享字段另一个不包含。
- 新通用 `/fields` 入口管理四类绑定基础值；原 `/text-fields` 仍能创建／维护 legacy 多行。共享字段的条目基础值可适配旧接口，字段名／类型／删除等全局操作必须走定义入口；旧 label PATCH 不能悄悄重命名全作品配置，可明确 409 引导新入口。
- 旧事件请求仍使用稳定 `custom_field_id` 与原字符串。新字段可复用事件路由，服务按真实定义判别 `value` 并进行 EntityRef↔handle 转换。
- `_event_out:844`、事件列表、变更结果与基础值输出都必须解码引用；不能只改 effective 输出却让事件详情泄漏内部形状。API 兼容不要求把原本不支持的新类型伪装为旧文本。
- `update_progression:951` 当前会将数据库 value 合并回公共 CREATE model。内部 handle 与外部 EntityRef 分离后，必须明确先解码再按公共请求合并，或使用专用内部命令；防止“只改备注”意外再次编码或把 handle 当实体 ID。

### 8. 按时点引用显示与 AI 注入

数据顺序应为：`主体基线＋事件 → 目标位置快照 → 选项／句柄身份解析 → 只读显示字符串 → 既有预算与去重`。先选中当时的引用身份，再解析其当前显示名；不先取最新引用，也不通过目标最新档案补齐源字段。

建议用共享服务一次批量取字段定义、选项及句柄，再格式化 `list[tuple[label,str]]`，让现有格式化层继续消费文本。角色、世界观、组织、职业、规划显式参考和分析都调用同一个格式器；纯 resolver 不访问数据库、不解析名称。

必须接入的位置：

- `_custom_labels:470`：替换 `.strip()` 和字符串默认假设；完整消费 typed snapshot，批量解析身份并形成显示文本，不把 dict 直接格式化。
- `effective_world_text:490`：继续使用目标章节快照，包含四类自定义值。
- `resolve_effective_cast:519`：在按历史成员／挂职确定相关组织和职业后，另批量加载组织／职业定义时间线，在**相同 StoryPosition**求值。可以给 EffectiveCast 增加按组织／职业 ID 的只读自定义字段映射，不给 ORM 当前行赋历史值。
- `format_organization:412`、`format_career_ladder:437` 与 `build_cast_section:668`：接收定义条目的本章自定义字段；保留既有相关性、裁剪顺序和预算。
- `planning_context_service._setting_items:347-421`：显式组织／职业即使没有任何涉及角色，也要独立求定义字段值。不能依赖 `resolve_effective_cast`（其 `:525` 在无角色时提前返回）。固定简介／体系仍标静态，自定义字段另标本章值；未绑定实际章节的分支继续只显示身份，不估章号。
- `analysis_service.build_analysis_job:724-748`：角色字段已通过有效投影；新增组织／职业值要显式接入相应块，不能误以为 context_builder 改完就自动注入。世界观目前没有独立分析消费入口，不应为满足回溯而无选择地把整作品世界观塞入分析；对子任务新增的分析设定块，应复用同一位置解析和已有相关性边界。
- `generation_service:280/312`、`rewrite_service:112`：已共用规划及 setting context；验证真实 messages，避免另造最新字段捷径。

引用只输出类似“驻地：世界观·青云山”的身份标签；不输出被引用条目的正文或自定义字段。A→B→A、自引用、引用链不导致递归、额外选角、授职、入组织或扩大规划保护集合。目标本来就在普通选角或显式规划中时，仍走它自己的相同时点投影与既有去重。

条目名和选项名使用当前整理元数据是本期明确边界，不代表名称也有历史版本。源字段第 20 章从 A 换为 B，第 5 章必须仍显示 A 的身份；A 后来被作者改显示名，回看第 5 章显示其当前名，不因此切换到 B。

缺失句柄／选项在正常写路径应被阻止。若导入损坏或异常数据导致缺失，读取应给可诊断警告／占位，不按同名或同号新条目猜测，AI也不递归寻找替代资料。

### 9. 适用类型、世界观子类型与配置变更保护

适用类型与时间线主体是两种维度：世界观六个子类型仍使用同一 `subject_kind=world`，字段定义通过 category 适用集合决定新绑定／可用性；不为每个子类型另造主体表，也不往旧 WorldCategory 枚举写自定义分类。

强制安全不变量：改变 `applicable_types`、引用允许类型、字段归档或 `WorldSetting.category` 时，不级联删除绑定、不清空 base、不停用事件，也不在回放时按**最新适用性**过滤掉旧字段。否则作者改一次配置，早期章节与 AI 会突然失去历史资料。

可行策略比较：

| 策略 | 行为与取舍 |
| --- | --- |
| 保留已有绑定（推荐的资料保护方式） | 新匹配条目得到只读空槽、首次写时建绑定；旧不匹配绑定保留并明确标记“保留字段”。重放按绑定身份进行，历史读仍完整；是否允许该不匹配字段继续新增事件需在设计中固定。 |
| 有资料就阻止缩窄／改子类型 | 发现非空 base 或任意保留事件则 409，提示先处理字段；实现简单、无隐式隐藏，但限制条目整理。 |
| 自动隐藏／删除不匹配字段 | 最省表单空间，却让最新元数据改写历史语义或丢资料。不能采用。 |

前两者都是可行产品行为；“保留并标记”优先，若本期不提供保留字段入口则宁可 409，不可静默隐藏。最终应把可观察行为写入子任务设计，不把研究建议当成用户已选择的交互。

`story_service.update_owned:138-157` 是必须补上的旧入口：仅 category 变化目前不触发字段校验／timeline CAS。应通过统一配置／绑定保护原语在项目锁内检查并变更，再保存 category；所有初始化、编辑、AI工具、批量操作不能各写一份适用性逻辑。`Career.kind` 的主副变更仍是既有职业逻辑，不等于自定义字段适用类型改变。

### 10. 必须涉及的实现文件与复用原语

| 范围 | 文件／原语 | 必要工作 |
| --- | --- | --- |
| 数据及迁移 | `models/progressions.py`、`models/__init__.py`、新 Alembic revision | 共享定义／选项／句柄、binding 关联与 JSON 基础值、主体形状与索引；保持旧 ID。具体迁移／备份细节由持久化研究负责。 |
| 公共合同 | `schemas/progression.py`、`api/v1/progressions.py`，可新增字段 schema／route | typed DTO、显式 null、SubjectKind、旧有效值及旧端点适配；路由仍只委托 service。 |
| 统一规则 | `setting_progression_resolver.py`；建议单独小型 `setting_field_service.py`／值合同模块 | canonical 解码／校验一个所有者，纯回放与显示查库分离，深复制对象型值。 |
| 基础与事件 | `setting_progression_service.py` | 复用 `_write_project`、`_claim_revision`、`resolve_timelines`、`apply_progression`、`revoke_progression`、`materialize_project`；扩展自定义数据装载、归属、类型、输出和新主体。 |
| 生命周期 | `story_service.py`、`organization_service.py`、`career_service.py`、`wizard_service.py` | 统一写时初始化；世界观改子类型保护；四类源条目删除、向导覆盖的 incoming 引用检查；空配置行不能冒充已有资料。 |
| 人工及 AI 上下文 | `context_builder.py`、`planning_context_service.py`、`analysis_service.py` | 同位置 typed 解析及统一格式化，组织／职业自定义值接入；保持未建章无猜测、不递归。 |
| 工具与撤销 | `agent/tools/write.py`、`agent/tools/references.py`、`agent/effects.py` | 去掉自定义值固定 text 假设；支持 null 清空；列出类型／合法选项／EntityRef；效果表范围、引用保护与拓扑恢复。 |
| 同步消费边界 | `setting_progression_service.latest_character_snapshots:316` | 当前返回 `str\|int` 且调用 typed map；卡片等消费方须按持久化研究的新合同适配，不能不经处理送旧卡片。 |

不要在本期重写职业挂接的 interval 验证、关系 lifecycle、章锚排序或另建递归引用框架。值／事件修改仍只 flush，由外层事务提交；只读查库保持 `no_autoflush`，整个失败连同版本递增回滚。

### 11. 有针对性的测试与证据入口

本轮只调研，未修改代码或运行产品测试；以下是实现必须验证的目标。

1. **纯求值**：扩展 `tests/test_setting_progression_resolver.py:33/:54/:106/:182/:224/:257`。四类分别覆盖第 20 章 end 变化，第 5、20 start 和第 21 start；多行追加／清空，单行替换及非法 append，下拉／引用替换 null，跨类型值、额外键、bool／越界 ID；乱序、孤儿、人工优先与 exclude 不变。修改返回的 option/handle dict 不能修改基线或事件。
2. **旧身份与 HTTP 兼容**：扩展 `tests/test_setting_progressions.py:180`，升级前后旧 field ID／事件 FK 相等，旧文本端点、string map、改名和 active 删除保护继续成立；新 typed 输出为完整数据，null 不被丢弃；同名 legacy 不自动合并；显式关联定义保留 ID；共享 label 不能经旧条目 PATCH 全局改名。
3. **主体隔离**：新增组织／职业定义字段集成测试，定义事件不创建成员／挂职，不改变主副职业上限；同一职业可同时有定义时间线和多个角色挂职时间线，无重复索引冲突；全部世界观子类型及新条目初始化覆盖。
4. **定义与引用生命周期**：改字段名／选项名／目标名，身份和值不变；退役选项可读且不能作为新选择；只在第 80 章、inactive 或 orphan 事件中引用的目标同样受保护；跨作品 target／option／binding 拒绝；清空当前值不能绕过历史引用；漏应用检查时 DB仍挡目标删除；整项目 PG／SQLite级联成功。
5. **配置保护与竞争**：模板缩窄／世界观子类型变化不删绑定，不因最新 metadata 过滤早期值；两页面相同 timeline revision 仅一次成功，定义改动后旧 def revision 拒绝；全部失败不部分写入、不递增有效版本。纯读取不创建空绑定。
6. **实际 AI 消息**：扩展 `tests/test_context_builder.py:262/:402`、`test_setting_context_preview.py:84/:215`、`test_planning_context.py:240/:326/:346/:412`、`test_progression_generation_paths.py:49/:69/:94`。普通角色／世界观及历史相关组织／职业、规划显式引用均取同章值；未建章无估计资料；四类字段在生成、重写、局部改写、分析消息中与预览一致，未来值不出现。循环引用不增加被引用正文、不扩大选角，已有预算和去重不变。
7. **工具与撤销**：扩展 `test_progression_agent_effects.py:74/:189/:239`、`test_progression_agent_gemini.py:9`。typed 选项／引用及 null 可通过实际工具 schema；恢复正确处理句柄及定义关联；有后续保留引用不能撤销其句柄／选项／字段；失败原子回滚，不能用裸 JSON 引用绕过 FK遍历检查。
8. **迁移与全量门禁**：新增真实升级和 `Base.metadata.create_all` 两套验证，PG与SQLite都核对旧 Text→JSON后的原文、主键、FK和约束；执行 `.trellis/spec/backend/index.md` 规定的后端 ruff＋pytest。备份、卡片、真实浏览器验收与另外两项研究合并，不在本文件假称已完成。

### 12. 相关规范

- `.trellis/workflow.md:383`：研究按主题持久化；当前只做规划。
- `.trellis/spec/backend/index.md`：后端层边界与质量门禁。
- `.trellis/spec/backend/setting-progressions.md:25`：稳定主体、基线／投影、章节锚、删除保护、项目锁、同事务及 no_autoflush。
- `.trellis/spec/backend/story-entities.md:209`：职业定义与挂接、稳定阶段身份、相关性及上下文裁剪。
- `.trellis/spec/backend/planning.md:47/:56`：公共 EntityRef、规划按目标章初、未建章不估历史、普通上下文去重。新增自定义字段后需要更新“组织／职业静态档案”的精确边界，不能声称其固定资料也时间化。
- `.trellis/spec/backend/database-guidelines.md:25/:69/:82`：异步显式查询、事务、项目隔离及父项目写屏障。
- `.trellis/spec/backend/quality-guidelines.md:79/:140`：隔离测试、跨用户 HTTP测试、迁移验证。
- `.trellis/spec/guides/cross-layer-thinking-guide.md`：复杂 JSON／事件合同只有一个解码和重放所有者，消费方只格式化。

### 13. 官方参考与版本

本地锁文件 `backend/uv.lock:853/:1151/:88/:425` 分别为 Pydantic 2.14.1、SQLAlchemy 2.1.6、Alembic 1.20.1、FastAPI 0.159.1；本研究不建议升级依赖。

- SQLAlchemy 官方说明 `JSON.none_as_null=False` 把 Python None 存为 JSON null，SQL NULL 另有显式表示，且 default 参数的 None 不表示 JSON null 默认。[SQLAlchemy 2.1 JSON 类型](https://docs.sqlalchemy.org/en/21/core/type_basics.html#sqlalchemy.types.JSON)（2026-11-05 查阅，页面为 2.1 系列，非项目精确锁版本。）
- Pydantic 的带判别字段联合可以按 Literal 选择模型并生成相应 JSON Schema；适用于新增完整 typed 字段 DTO。[Discriminated unions](https://pydantic.dev/docs/validation/latest/concepts/unions/#discriminated-unions-with-string-discriminators)（2026-11-05 查阅。）

这些官方资料只支持序列化／校验机制，字段语义、兼容投影和删除策略仍是本项目设计建议，不是第三方文档保证。

## Caveats / Not Found

- 未发现作品级字段定义、稳定下拉选项、引用句柄，或组织／职业定义自身时间线；它们均是新增建议，不能作为既有能力引用。
- 名称使用当前整理元数据已写入父 PRD，本期不扩展名称历史；不能在交付中声称“过去显示名也逐字还原”。
- JSON 中的 handle/option ID 没有数据库直接 FK，统一删除／恢复服务仍是必要边界；句柄只提供目标不被误删的数据库兜底。
- 类型化基础值迁移会碰到 PostgreSQL JSON 转换及 SQLite父表重建／子行保护，详细方案与持久化研究合并；本研究未独立运行迁移或确认任一 DDL 已可执行。
- 共享定义适用范围缩窄后保留字段是否继续可写属于最终设计的可观察行为，不能通过最新配置过滤偷偷决定；至少必须保留历史，或明确 409。
- `_custom_labels`、`latest_character_snapshots`、智能体 `text` 值和 `EffectiveOut` 的字符串假设是最容易漏掉的四个消费点；`update_progression` 的公共／内部引用转换也必须单测。
- 不重新探索前端、项目备份或卡片实现；仅标记与这些链路的必要接口交接。未读 implement/check JSONL，未修改产品代码、规范、其他任务或 Git 状态。
