# Research: 设定演变设计的有界集成审阅

- Query: 审核当前 `design.md` 在 M1 升级、新旧创建路径、章节插改删、备份 v2／卡片 v3、来源 FK、locked 提示词及智能体效果恢复上的一致性。
- Scope: internal；基于既有 `integration-and-persistence.md`，仅对明确疑点查看源码；不修改设计或产品代码。
- Date: 2026-11-03
- Reviewed: `design.md` 本次审阅版本；最多 5 项，本报告共 4 项需要在进入实现前补齐的集成契约。

## Findings

### 1. [P1] M1 新建的基线与升级前已保存的智能体 effects 没有兼容约定

**设计位置**：`design.md:156` 为现有实体创建 M1 基线；`:160` 要覆盖智能体效果恢复；`:172` 要恢复新事实表并重算。设计已为旧 `AnalysisChange` 定义兼容闸门（`:114`），但未对旧 `AgentChange.effects` 定义等价规则。

**源码事实**：

- `backend/app/models/agent.py:61` 将效果保存为 JSON 列；旧记录不可能带本次新增基线／时间线表。
- `backend/app/services/agent/service.py:568`–`:581` 直接逐项将已保存 `row.effects` 交给 `effects.undo`，没有效果结构版本或升级代次判断。
- `backend/app/services/agent/effects.py:234`–`:254` 扫描新建实体的全部 FK 依赖；M1 迁移新增的 timeline 会被旧撤销视为“后来新增的关联”，导致原先可撤销的新建操作突然 409。
- `backend/app/services/agent/effects.py:319` 的 Core 恢复只写 effects 内的行；旧删除效果恢复角色时不会带 timeline，旧修改效果恢复 `current_state` 时也不会更新 M1 baseline。

**实际结果**：旧修改撤销若仍执行，会出现实体当前值被恢复、timeline 基线却仍是升级时新值，下一次 materialize 又把撤销覆盖；旧删除撤销可能恢复出没有 timeline 的实体，而设计禁止 GET 懒补；旧新建撤销则被 M1 自身创建的依赖误判为用户新修改。

**最小修正建议**：在设计中明确 effects schema／timeline 支持版本。升级前涉及时间线主体的 effects 选择一种受控路径：

1. 安全兼容路径：仅在目标字段没有新剧情事件且现值冲突检查通过时，转换为 M1 基线修正／恢复，保持 `legacy_unverified`；恢复实体时显式补同事务基线；本次迁移产生的依赖需能与用户后来新增依赖区分。
2. 较小实现路径：把这类旧 effects 标记为不可直接撤销，返回明确 409 并引导使用新的基础设定修正；不影响纯正文等无关旧 effects，也不删除历史。

无论采用哪种，不要在未知旧 effects 上无条件执行 Core 恢复再 materialize。

**验收补充**：0030 数据库预置已执行的角色创建／状态修改／删除三类 AgentChange → 升级 → 分别撤销，验证结果要么符合明确兼容规则，要么安全 409；不存在当前值与基线分叉、无 timeline 实体或误删后加事件。

### 2. [P1] 备份需导出仅被历史基线／事件引用的全局关系类型

**设计位置**：`design.md:170` 要映射 baseline/value 中 type ID 并保证 v2 完整往返；目前未明确扩大关系类型的导出引用范围。

**源码事实**：

- `backend/app/services/project_transfer/registry.py:146`–`:153` 仅导出本项目自定义类型，以及当前 `Relationship.type_id` 引用的全局预置类型。
- `backend/app/services/project_transfer/importer.py:740`–`:756` 只为备份文件中出现的类型建立旧 ID → 新 ID 映射。
- JSON remapper 的缺失引用处理会得到 null／缺失警告，不能凭原库整数 ID 跨实例恢复（`backend/app/services/project_transfer/remap.py:64`–`:76`）。

**具体例子**：基线为全局“朋友”类型 A，第 20 章变成全局“敌人”类型 B，当前载体 `type_id=B`。现有 scope 仅导出 B；timeline 的基线 A 即使经过正确 remapper，也找不到映射，导入后早期关系类型丢失。这与新表是否登记 RULES 无关。

**最小修正建议**：显式写入“备份引用闭包”：关系类型导出集合还要包含所有保留 timeline baseline、存在性快照及事件 `value` 引用的 type IDs，包括 inactive／孤儿记录中的引用（设计要求保留并可维护这些记录）。若采用引用辅助表，registry 可经其 FK 收集；否则 exporter 预收集结构化 JSON 引用。映射失败应拒绝不完整的 v2 核心事实，不可把所需 type ID 静默置空继续导入。

**验收补充**：A→B 且当前只引用 B 的全局类型，在两个用户／实例间 v2 往返；章 5 保持 A、章 20 后保持 B；含仅在 inactive 事件中的类型时也保留编辑可用性。

### 3. [P2] `is_present` 需要两套显式读取语义，当前直读入口和卡片复建必须列入实施清单

**设计位置**：`design.md:87` 说明 list/graph/relations_among/find_pair 等默认过滤，隐藏关系复建复用载体；`:171` 要卡片导出最新快照。总体规则正确，但具体直读入口与底层存在性检查容易落空。

**源码事实**：

- `backend/app/services/open_api/entities.py:167` 直接查询 Relationship 并计算分页总数，没有经过 `relationship_service.list_relationships`；`:195` 的单条开放读取同样需要统一当前可见语义。
- `backend/app/services/card_service.py:104` 直接 join Relationship 与类型导出关系。只修普通关系 API 会让“已不存在”的隐藏载体仍进入 v3 当前快照。
- 卡片导入在 `card_service.py:521` 用 `find_pair` 判断跳过，随后 `:524` 调 `create_relation`；`relationship_service.py:212` 当前 find_pair 同时服务业务存在性和底层唯一性检查，`apply_change` 在 `:292` 也调用它。
- 数据库仍有 pair_low/pair_high 唯一约束。如果 find_pair 默认过滤隐藏行，而 create_relation 仍按“未找到”插新行，就会在复建／卡片导入撞唯一约束。

**最小修正建议**：明确命名／参数区分“当前存在关系”与“稳定载体含隐藏关系”，例如 `find_current_pair` 与 `find_pair_record`，不要让一个默认过滤函数同时承担唯一性检查。将开放 API 的列表／详情／总数、卡片导出、卡片导入、AI 缺失关系生成及通用创建分别列入检查矩阵。备份与效果快照则必须包含隐藏载体及其历史，不能复用 current-only scope。

**验收补充**：关系 exists=false 后，普通列表、图、开放 API 与卡片快照都不出现；项目备份仍包含载体与历史；再次显式建立同对关系和卡片导入不会唯一冲突，既有历史不被删除。

### 4. [P2] 分析来源 FK 的删除动作与“可选分析日志”需要准确固定

**设计位置**：`design.md:67` 提及 nullable `source_change_id`，`:173` 只描述导出时可置空。章节锚点已明确 SET NULL，但来源 FK 尚未明确 ondelete。

**源码事实**：

- `backend/app/models/entities.py:658`：删章节会 CASCADE 删除 ChapterAnalysis。
- `backend/app/models/entities.py:691`–`:693`：删 ChapterAnalysis 又 CASCADE 删除 AnalysisChange。
- 目前 `chapter_analyses` 与 `analysis_changes` 都是核心导出表，无 option（`project_transfer/registry.py:254`、`:276`）；可选的是 `generation_logs`（`:235`），不是分析结果。因此当前“可选分析日志导出策略”并不存在。

**风险**：只让 `source_change_id` nullable 却不给删除动作，默认 NO ACTION 可能让删除章节失败；若误选 CASCADE，原本应保留为孤儿或已人工重绑的剧情事件会随来源审计被删。来源导出／删除行为也不能与 generation_logs 混为一谈。

**最小修正建议**：明确 `source_change_id → analysis_changes.id ON DELETE SET NULL`，来源可空不影响事件事实、来源类别与顺序；普通 v2 备份维持分析结果／回写条目作为核心数据并映射来源，不新增未获范围授权的导出开关。来源已不存在时保留独立事件并禁用原分析按钮撤销，设计已有这一方向，补上 FK 与测试即可。多字段事件组若后续仍需作为一组操作，应有不依赖可空来源 FK 的稳定组标识，否则明确来源丢失后按独立事件维护。

**验收补充**：删除源章节后事实按锚点规则成为孤儿；手动已重绑到其他章节的事件只失去分析来源、仍有效；删 generation_logs 不影响分析来源；v2 往返时有来源则正确映射、无来源仍可维护。

## 已核对、未发现额外阻塞的问题

- 已有章节使用 ID、planned 章号单独入 CHAPTER_NO_REFS、SET NULL 孤儿不按同号复活，这套锚点设计可覆盖现有插章／直接改号／普通删章与细纲删章差异。
- 设计已要求批量插改删末尾在同事务重算、检查全受影响区间，能够避免位置变化后最新投影陈旧；实现清单应保留四个入口，不需要再引入另一套章节排序系统。
- 局部改写使用新 locked 块、空块保持 parity、旧已保存覆盖运行时兜底、编辑／导入／恢复时要求补占位符，与当前 templating 行为一致。通用兜底本身只负责缺失时补入，不修复任意手工损坏的覆盖；无需为此扩展本任务。
- M1 不重放旧历史、v1 项目导入按 M1、v2 保留校准状态、v3 卡片作为新作品基线，三者可以共存。实现必须给“新建／v1 初始化／v2 完整恢复／effects 恢复”明确模式，避免通用 confirmed 初始化覆盖导入的 legacy_unverified 状态；当前设计已有保留状态要求，不另列问题。
- 成员状态纳入同一时间线、legacy 关系备注按是否采用结构化管理决定回退，解决了上一轮发现的主要上下文旁路。

## Files Found / Related Specs

- `design.md`：本次审阅对象，以上引用为本次读取时的行号。
- `research/integration-and-persistence.md`：前序调用链和持久化证据。
- `.trellis/spec/backend/project-transfer.md`、`.trellis/spec/backend/agent.md`、`.trellis/spec/backend/prompts-styles.md`：对应备份、撤销、提示词契约。

## External References / Versions

无新增外部依赖、外部事实或网络研究；源码当前 DB 迁移头为 0030，目标方案拟新增迁移并升级项目备份至 v2、卡片至 v3。

## Caveats / Not Found

- 这是规划级问题，不是已实现代码的缺陷报告；修正以上契约后即可继续实施规划。
- 未重复全面探索、未修改设计／PRD／产品代码、未运行测试。
