# 类型化设定字段：技术设计

状态：2026-11-05 用户在最终摘要后回复“开始”，设计已批准实施。需求见本任务 PRD；源码依据见父任务 `research/typed-field-timeline.md`、`persistence-and-writers.md`、`fields-categories-ui.md`。本设计固定实现合同，研究中的其他备选不作为实施默认。

## 1. 边界与身份

扩展既有时间线而不再造事件表。保留 `setting_text_fields` 的物理表名、主键及事件 `custom_field_id`，Python 可增加领域中立别名，避免无收益的全库改名。

字段适用类型为 `character`、`world.place/faction/item/rule/history/other`、`organization`、`career`。现有世界观固定类型不改，角色定位、组织 org_type、职业主副修不是新的配置命名空间。

时间线新增 `subject_kind=organization`（只有 organization_id）和 `career_entry`（只有 career_id），两者均不超过现有 String(16)。原 `career` 仍是角色＋职业挂接，`membership` 仍是角色＋组织。新主体的内置 baseline 为 `{}`，仅管理自定义字段，不参加授职、成员或阶段数量约束。

## 2. 数据模型

| 模型／修改 | 权威数据与约束 |
| --- | --- |
| `SettingFieldDefinition`（新） | project_id、稳定 id、label、field_type、applicable_types、reference_types、sort、archived。名称在作品内唯一；类型为 text／line／dropdown／reference；适用和允许引用类型是有界枚举数组。 |
| `SettingFieldOption`（新） | definition_id、稳定 id、label、sort、archived；同定义内名称唯一。停用保留旧身份，不能用下标或显示名作值。 |
| `SettingReferenceHandle`（新） | project_id、kind、四选一 character_id/world_setting_id/organization_id/career_id；目标 FK 为 NO ACTION，project FK 为 CASCADE；形状 CHECK 和每类型唯一索引防重复。新身份不可被删除后复用。 |
| `SettingTextField`（保留） | 新增 nullable definition_id；base_value 从 Text 转 JSON；label 可空。legacy 行 definition_id=NULL、label 非空；共享绑定行 label=NULL，名称与类型从 definition JOIN。保留旧 timeline_id+label 唯一，新增 timeline_id+definition_id 唯一。 |
| `SettingTimeline`（扩展） | 新主体形状 CHECK／部分唯一索引；revision 和旧基线状态不变。迁移为已有组织／职业创建空字段时间线，不把静态档案搬成历史。 |
| `Project.fields_revision`（新） | 字段目录 CAS 版本；修改定义／选项／适用范围递增，与 timeline revision、planning revision 分开。 |

定义负责配置，绑定负责单条目的起始值和事件。不存在全作品共享值或自动非空默认值。

### 空字段与旧字段

- 读取按适用定义 LEFT JOIN 实例，未填写的共享字段显示为空槽 `definition_id`＋`field_id:null`；读取不得建绑定、时间线或句柄。
- 首次写基础值，或首次直接记录章节变化时，在项目事务内创建空绑定，然后写值／事件；返回稳定 field_id。不能将 definition_id 当成 custom_field_id。
- 旧行 `definition_id=NULL`，原文逐字转为 JSON 字符串；不按名称聚合。共享字段与同名旧字段可共存，UI 标明来源。
- 作者可显式把旧字段关联到兼容的 `text` 定义：只改 definition_id 并将本地 label 置 NULL，保留 ID、base_value、事件、来源与时间戳。目标已有绑定时 409，不自动合并两份资料。

## 3. 值、空值和 codec

定义元数据是类型权威。内部基础值和事件使用相同 JSON 形状：

| field_type | 内部值 | 外部值 | 允许操作 |
| --- | --- | --- | --- |
| text | string | string | append／replace；replace 空串清空，append 需非空内容。 |
| line | string | string | replace；空串清空，拒绝 CR/LF 和 Unicode 行分隔符。 |
| dropdown | `{option_id: positive_int}` 或 JSON null | 同内部 | replace／null 清空。 |
| reference | `{reference_handle_id: positive_int}` 或 JSON null | `{kind, id: positive_int}` 或 null，kind 为四类实体之一 | replace／null 清空。 |

- 公开 DTO 使用 `field_type` 判别联合及 value，后端再与定义核对。引用 codec 负责 EntityRef ↔ handle，不把内部句柄暴露为产品概念。
- ID 是严格正 int32，拒绝 bool／数字字符串／越界；对象只允许约定键。单行按 Unicode 码点限制 500 字；多行保留原有长度与格式边界，不为兼容迁移裁剪旧值。
- 事件创建必须带 value；PATCH 省略 value 是不变，显式 null 仅清空 dropdown/reference。使用 exclude_unset，不能用 exclude_none 丢弃清空。
- JSON 配置明确 `none_as_null=False`，数据库列保持 NOT NULL，空选择是 JSON null，不是 SQL NULL。
- 一个共享 codec／validator 负责形状和操作，服务负责归属／可选状态，纯 resolver 使用预取 FieldSpec。对象值深复制，不能沿用仅适合字符串的浅复制或 `str(value)`。
- 公共有效值新增 `field_values` 类型化结果；旧 `custom_fields: dict[int,str]` 保持多行文本兼容投影，不向其塞对象。旧 `/text-fields` CRUD 只修改 legacy 行，不能通过单条编辑改变共享定义。

## 4. 回放与历史依赖

继承当前章锚、章初／章末、排序、孤儿排除、人工保护和版本逻辑。`position=None` 应用全部有效事件，包括计划章变化；UI 的“当前”不等于最后写作章。

- 新选择只允许当前可用选项及允许的目标类型；旧值回放按稳定身份合法性判断。停用后的旧值可查看、原样保存和调整事件备注／章锚，不强迫清空；重新改变 value 时才执行新选择规则。
- 下拉比较 option_id，引用比较稳定 handle 身份；改显示名不改生效位置。字段、选项、条目显示名仍用当前元数据，不增加名称时间线。
- 已有绑定／事件／分类规则引用后禁止原地转换字段类型；缩小适用类型或引用类型若会排除已保存内容，或使任一已存分类规则与字段适用类型失去交集，则 409，保留数据。字段服务提供统一依赖校验，分类子任务把规则接入 UPDATE 和 DELETE 两种保护；人工 PATCH、导入和 effects 恢复同样适用。世界观旧 PATCH category 同样执行适用性校验，不能只在新字段入口检查。
- 定义／选项有值、事件或后续分类规则引用时不能硬删；停用不移除既有字段和历史。删除 legacy 字段保留原有效事件保护语义；typed 字段删除不能静默清理引用历史。
- 引用目标单删、向导覆盖、效果撤销先检查所有保留基础值和事件，含 inactive／孤儿。仍被引用则 409 并指出来源；目标 FK 防遗漏删除。句柄无普通删除 API，仅在同项目锁内证明无任何引用时内部清理。
- 自引用／循环引用可以保存，因为没有递归取档案。整作品删除级联清理全图；必须验证 PostgreSQL 和 SQLite 的 NO ACTION 行为，不能换成 RESTRICT 或 SET NULL。

## 5. API 和并发合同

路由前缀 `/api/v1/projects/{pid}`，沿用项目 snake_case、items/total 分页及 AppError：

| 路由族 | 功能 |
| --- | --- |
| `/setting-field-definitions`、`/{definition_id}` | GET 列表／详情、POST 创建、PATCH 更新、DELETE 无引用定义；读写携带 fields_revision。 |
| `/{definition_id}/options`（接上路由） | 下拉选项创建／编辑／排序／停用；稳定 ID，字段目录 CAS。 |
| `/setting-entries` | 只读跨类型目录；types、q、page、page_size，供选择器和后续分类复用。 |
| `/setting-entries/{kind}/{id}/fields` | 返回 timeline_id、timeline_revision、fields_revision、position、类型化字段槽及 legacy 字段。位置与既有章节参数同义。 |
| `/setting-entries/{kind}/{id}/fields/{definition_id}/baseline` | PATCH 指定共享字段起始值；首次写原子建绑定。 |
| `/setting-entries/{kind}/{id}/fields/{definition_id}/progressions` | POST 首次或后续共享字段变化；原子 ensure-binding＋apply_progression。 |
| `/setting-entries/{kind}/{id}/legacy-fields/{field_id}/binding` | 显式关联共享定义，保留字段身份；需要目录和时间线版本。 |
| 旧 `/setting-timelines/...` | 保留原 legacy 写法；新客户端的既有事件修改／删除复用此路径的类型化扩展。 |

写值／事件带 expected_timeline_revision 和 expected_fields_revision；目录操作带 expected_fields_revision。所有影响在既有项目 active 写锁和一个事务内完成；底层只 flush。旧文本入口保持旧 timeline CAS，同时不能绕过新保护。

### 旧事件路由与编解码

- 旧事件 CREATE／PATCH／DELETE 保留原 `expected_revision` 参数名；新复合入口的 expected_timeline_revision 映射到同一个内部 CAS。旧内置字段／legacy 请求仍按原合同工作。
- 旧路由为新客户端新增 expected_fields_revision；操作共享绑定事件时必须携带并检查，缺失明确拒绝，不允许从旧路由绕过目录 CAS。legacy 文本和内置事件不强迫旧客户端新增参数。
- PATCH 使用内部 canonical 命令合并：value 未提供时保留原 canonical 值；明确提供时按目标 FieldSpec 将公共值编码一次，再统一校验。不能将数据库 handle 对象直接当公共 EntityRef 重新编码，也不能因只改 note／章锚而强迫重选停用值。
- 事件列表／详情／创建更新返回、基础值和 effective 都走同一公共投影解码，内部 reference_handle_id 不从任一路径泄漏为公开值。

### 读数据与版本的一致性

目录读先取 fields_revision，完成元数据查询后再核对；条目字段读同时核对前后 timeline revision。任何版本变化返回 scope 明确的冲突，或在独立干净读取中重试，绝不把旧定义／选项配上最后读到的新版本。上下文预取遵循同一合同。分类等跨批次复合查询使用一个只读一致快照，不能靠最后一次读取目录版本伪装整体一致。

冲突矩阵：不可见主体／目标 404；只读动作 403；结构和类型 422／400；版本不符 409 stale_revision（明确 scope）；在用定义／选项／目标及不兼容换类 409 的各自 code。所有冲突先回滚，不部分更新，也不把所有 409 当版本冲突。

## 6. 上下文与既有写入口

新增批量 `resolve_entry_fields(entries, position)` 及统一格式器；共用 resolver、批量定义／选项／目标预取，避免 N+1。其结果同时供详情、分类、上下文与卡片读取。

- `context_builder`／`planning_context_service` 在既有选中实体范围内附字段；相关组织与职业定义按同一目标位置取值，不从人物挂接上取。
- `analysis_service` 现有角色字段、相关组织和职业块补同一格式器；世界观按原分析选择范围接入，不能借本任务扩大为全书全部档案注入。
- 引用只输出类型与当前名称，不递归读目标正文、不因引用自动加入角色或改变事实。所有新增字段计入原有上下文预算和裁剪，不绕过显式参考保护或锁定块。
- 新建实体走统一初始化：手工、with-relations／members、职业 batch、向导、拆书、卡片。无值字段仅显示为空，不为全部条目制造有内容的保护记录。
- 旧世界观 category 修改、实体删除、向导准入和落库前检查统一保护。开放 API 保持原独立白名单，本期不承诺其新增字段或历史读取能力。
- 智能体读写、引用白名单、provider 工具显式类型、预览 ID 归一和效果恢复一起扩展；旧效果按版本整轮预检，缺新资料图信息时拒绝不安全恢复，纯正文旧效果仍可用。

## 7. UI

- 新懒加载 `setting-fields` 配置页，列表按适用类型查看；配置定义、选项、顺序、引用范围和停用，不把条目值放进配置。
- 角色／世界观复用独立字段区及变化面板；组织／职业增加独立主体入口，和成员／阶段／挂接保持分开。
- 一套类型控件用于起始值、事件和只读值；输入分别为 TextArea、Input、Select、跨类型分页单选。复用并抽取现有规划条目选择器和四类型详情 URL helper，不依赖规划业务版本或标签。
- 空槽首写走复合事务 API；旧字段标明“本条目原有字段”，显式关联配置提供前后摘要。
- 外层资料表单、字段起始值、事件、配置各自保存，刷新不覆盖其他草稿。共享 useLoader／身份与请求代次；目录变更需重取元数据并比较，不自动改 revision 后覆盖。
- 引用候选搜索失败保留原值，可重试；停用选项的已存值仍显示文字状态。只读可浏览配置、值和变化；前端及后端均校验真实权限。

## 8. 迁移、备份、卡片及回退

- 计划迁移 `0035`（实施前再核对 head）。旧 base_value 转 JSON 字符串，保留每行 ID／时间戳／FK 和事件；SQLite 父表重建必须验证有真实子行时不级联误删。
- project v4／APP_SCHEMA 0035，接受 v1–v4；新定义→选项、四实体→句柄→时间线→绑定→事件按拓扑登记到 RULES，新增 JSON refs 用精确语义 remap。保留全部历史引用闭包，缺核心引用整笔拒绝。
- cards v4：定义／选项／实体使用包内 ref，不带源数据库身份；保留现有角色／组织／职业快照范围，包外目标在预览要求显式映射。现有卡片没有世界观容器，世界观目标视为外部引用，可映射现有条目；不伪造空壳或偷偷复制整书。
- 新卡片里实体先建立，再映射引用和字段，允许自引用／循环。定义冲突在预览显式复用兼容定义或改名，既有实体选择跳过时不覆写其值／历史。v4 包内被引用实体若遇同名冲突，必须选择重命名新建或明确确认复用的具体目标；默认 skip 仅跳过内容，不解析新 typed reference，未确认引用进入映射清单。不能复用旧 char_map 的默认按名关联作为引用授权。缺映射、跨作品或不兼容配置则整笔回滚；v1–v3旧关系行为保留。
- effects 格式升 v3，注册新表、JSON 依赖检查和语义预览；fields_revision 从 PROJECT_RULE 导出列以及 effects 自己的项目列快照／恢复集合两处排除，不复制源作品代次或写回 effect.before 中的旧值。恢复在项目锁内从当前数据库高水位递增受影响目录并重验字段图；整轮失败不递增。
- 降级只允许旧版可表示的空态；存在新定义／类型／引用／主体资料时明确拒绝。需要恢复旧版本时使用升级前备份，不能把对象转文本冒充无损。

## 9. 交接

字段目录／EntityRef／批量时点投影／值格式器和版本合同完成并验证后交接分类。分类只能通过该接口判断下拉有效值，不能直接读取 base_value 或当前投影。
