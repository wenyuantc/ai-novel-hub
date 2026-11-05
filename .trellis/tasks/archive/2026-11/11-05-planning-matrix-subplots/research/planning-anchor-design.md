# Research: 规划单元的稳定锚点设计

- Query: 在已有章节＋未建章细纲之间保持规划身份，兼容删除、重建、快照、并发和备份，且不丢作者规划。
- Scope: internal
- Date: 2026-11-05
- 已确认范围：新增 POV／支线接入生成和重写；覆盖已有章节与未建章细纲，建章保留；手动关联覆盖角色、全部 WorldSetting 分类、组织和职业。

## 结论与方案比较

**推荐独立 `PlanningUnit`，新增规划数据全部引用 unit id。章节与细纲是它当前挂接的来源，而不是规划内容的主键。** `chapter_id`、`plan_id` 均可空且 `ON DELETE SET NULL`；两个非空时必须是当前双向关联的同一章／细纲。二者都空表示“待重新关联”，规划内容和节点仍保留。

| 方案 | 简单处 | 真实成本／风险 |
|---|---|---|
| 直接给 Chapter、ChapterPlan 各加一套元数据，节点保存二选一 FK | 无独立规划单元 | 建章要搬运每一种字段与节点；已有章复用冲突分散；删源后无处保留；快照按原 plan id 重建可能错误认领；每增加一种关联都要改双套生命周期。 |
| 独立 PlanningUnit＋两个可空来源 FK | 新增元数据、关联与节点只认一个稳定 id | 需要集中实现挂接／脱离／建章冲突处理，以及少量现有生命周期调用点。复杂度集中且可测试。 |

这不是把未建章细纲强制转换为正文空章：原有 Plan 可以仍未建章，矩阵显示“某大纲 · 第 N 条细纲”，不伪造确定章号。依据 `backend/app/models/entities.py:504` 和 `backend/app/services/plan_service.py:73`，未建章 Plan 没有真实章号。

## 最小模型与不变量

- `PlanningUnit`：`id`、`project_id`、可空且唯一的 `chapter_id`／`plan_id`、`pov_character_id`、标签、最后可读来源名称／分卷信息、只在脱离后使用的角色列表快照。新增元数据允许单独表，不要求全部塞一个 JSON。
- 节点使用 `unit_id` 真外键；无须同时再存 chapter_no、plan_id。顺序从当前来源动态读取，避免重复持久化故事位置。
- 新表建议使用 `sqlite_autoincrement=True`；项目已有该模式，见 `backend/app/models/rbac.py:11`。隐式删源不得删除 unit；空的脱离 unit 可隐藏，不需要首版提供硬删除它的接口。若未来允许清理，应继续保证 id 不复用，避免快照中的旧 unit id 指向新记录。
- 每章／每细纲最多一个 live unit。唯一 FK 约束兜底；服务层验证同项目、双向章／细纲链接一致。两个来源都非空时优先作为已建章行展示，不能重复展示一行章、一行细纲。
- **保留来源名称不是保留可自动认领的旧来源 ID。** 不用 `last_plan_id`、同章号、同标题等启发式自动重连；旧 ID 仅可作为审计显示，不能做写入定位。
- 全部新关联必须有可校验的类型：推荐 `PlanningUnitWorld`、`PlanningUnitOrganization`、`PlanningUnitCareer` 三种明确 join 表，复合主键 `(unit_id, entity_id)`、目标实体真 FK＋CASCADE；WorldSetting 自带分类，不再分六张表。角色沿用下述已有事实源，不新增 live `PlanningUnitCharacter` 副本。也可采用单表三个 typed FK＋恰有一个非空 CHECK，但独立 join 的导入／外键规则更直观。
- 新的 `pov_character_id` 是单独语义的 FK，不取代出场角色；POV 删除行为 SET NULL，关联列表中的角色删除仍按原有规则过滤／级联。

## 角色的单一事实来源

不能再复制一个“规划角色列表”长期与现有两种列表各自演进。

1. **已建章 unit：`ChapterCharacter` 为权威。** `backend/app/services/cast_service.py:121` 已把它用于 AI；`set_cast` 在 147 行整体替换并校验同项目。
2. **未建章 unit：`ChapterPlan.character_ids` 为权威。** 建章之前没有 ChapterCharacter 可写。
3. **章与细纲已关联：Plan.character_ids 是兼容镜像，不是第二份可独立编辑的选择。** 所有既有入口统一经过 flush-only 内部 helper：修改章 cast 后同步 plan；修改已建章 plan.character_ids 时实际修改章 cast 再同步镜像；读取 PlanOut 返回同一权威值。不能直接调用现有会 commit 的 `set_cast` 嵌进批量事务。
4. **建章时第一次合并** 保留当前已有行为：现有章 cast 与待建细纲角色取并集，见 `backend/app/services/plan_service.py:225`、`262`、`295`；然后写回 plan 镜像。对于已有历史数据，两者可能不同；不在迁移时全库粗暴覆盖。首次相关写入／建章时确定章为 live 权威并规范化镜像；只读旧数据仍可展示细纲旧值为“历史细纲建议”提示，避免误称同一事实。
5. **删章但细纲仍存在**：在删章前把当前 cast 复制到保留的 plan，随后 plan 重新成为权威。**来源全部消失**：复制到 unit 的 `detached_character_ids`，仅作为待重新关联规划的权威列表；活跃行不读取这个字段。重新挂接成功后把它写入目标权威源并清空。该 JSON 必须登记角色 id remap，过滤真正已删除的角色。
6. **恢复大纲快照** 不把历史 Plan.character_ids 强行覆盖仍活着的章 cast：恢复链接后让 plan 镜像采用当前章权威；否则恢复一份大纲会悄悄改变 AI 选角。

已有差异的代码证据：`plan_service.py:104` 的 update_plan 当前只改 Plan，不改 ChapterCharacter；`cast_service.py:147` 当前只改 ChapterCharacter。这两个入口必须一起纳入设计，不能只实现矩阵入口。

## 建章及来源变更

### 正常建章与复用已有单章

`backend/app/services/plan_service.py:236` 是集中接入点。251–268 行可能把同大纲已有的单章复用为第一条细纲，之后才创建其余章。

- 只有 plan unit：保持 unit id，设置 chapter_id，并保留 plan_id；全部 POV、标签、手动关联和节点原封不动。
- 只有 chapter unit：保持其 id，设置 plan_id。
- 两边均没有 unit：无需只为建章创建空记录；矩阵继续返回虚拟来源行。
- 两个 unit 同时存在但一个完全没有作者规划／节点：保留非空者，空者脱离并隐藏，返回最新 id；不删除可能被快照引用的空 unit。角色按既有并集合并。
- **两边都有作者规划：不得静默选一边或覆盖 POV／节点文字。** 在真正写入任何章之前预检整个建章批次；存在冲突就 409，整批保持原状。提供冲突 unit 与字段，让作者在矩阵中明确合并／移动节点后重试。最小可落地的冲突解决是一个显式合并动作：CAS 两个 unit 与目标挂接、选择单值 POV、集合型关联取并集、移动节点保留 node id；如业务限定每支线每章一个节点，同支线双节点必须由作者合并文字，不能自动删除其中之一。

单值冲突不是靠数据库唯一错误临时处理；建章前必须给可理解的错误。不能为了绕开冲突把旧章节规划自动藏到“待重新关联”，那会悄悄改变该章下一次生成的上下文。

### 生命周期决策表

| 既有操作 | unit 结果 | 必须接入的位置／依据 |
|---|---|---|
| 删除章，原细纲仍在 | 保留同 unit 与 plan_id；章 cast 转存 plan，chapter_id 置空 | `chapter_service.py:234`，当前 Chapter 删除后 Plan.chapter_id SET NULL |
| 删除无细纲的章 | unit 变待重新关联，全部规划／节点保留，保存来源名称及角色快照 | 同上；ChapterCharacter 原本会 CASCADE |
| 删除细纲，章仍保留 | unit 保留 chapter_id，plan_id 置空 | `models/entities.py:493`、`507` |
| 重新展开移除细纲及对应空章 | 删除前统一脱离两种来源，保留同 unit 为待关联；新细纲不得按复用 ID 接走它 | `plan_service.py:163`、`187`、`193`，当前会删除空章并平移后续章号 |
| 重新展开保留提交中的既有 plan id | 同 unit 留在原 plan，仅 seq／内容变动 | `plan_service.py:203`–214 |
| 删除／导入替换／向导覆盖大纲 | 未建章 unit 脱离；仍活着的章 unit 留章；批量操作前保存必要角色／名称 | `story_service.py:161`；`outline_transfer_service.py:153`；`wizard_service.py:616` |
| 章改挂另一大纲并解除 plan | 原 unit 随章，plan_id 清空；旧 Plan 回未建章，独立新规划为空 | `chapter_service.py:195`–204；不要复制同一支线推进到两条故事线 |
| 改章节号／细纲 seq／大纲 sort | unit id 与 node id 不变；动态排序／统计刷新 | `chapter_service.py:209`、`outline_service.py:97`；后者只改 sort |

脱离 helper 必须在源删除前运行，FK SET NULL 只是兜底，不能承担保存 cast／来源标题等逻辑。建议中央 helper 不 commit，由既有事务所有者提交。对一次重新展开中的多次删章，全部位置变动完成后再统一重排视图数据，不在中间状态做关联猜测。

## 大纲快照：同整数 ID 并不证明同一来源

### 现有危险点

- `outline_snapshot_service.py:244` 先删除所有大纲／细纲，再重建。
- `outline_snapshot_service.py:226` 尽量用原整数 id；若被占用才分配新 id，并用内存 old→new 映射重连。
- `backend/tests/test_outline_snapshots.py:118`、145 已覆盖 SQLite 复用已删除 ID 的真实案例。
- 快照恢复不修改章节号（模块说明 15 行），因此用章号或 title 重认领都不可靠。

### 推荐扩展

1. 新快照只增加明确的 `planning_bindings: [{unit_id, plan_id}]`。保存当前规划单元的**绑定关系**，不复制支线定义、节点、POV 等新内容；大纲恢复不应暗中回滚后续规划修改。
2. 恢复前执行统一脱离 helper；然后按现有方式创建 outline／plan，并取得 old plan id→实际新 plan id。
3. 优先保留仍存在 Chapter 的 unit；对快照 binding，验证 unit 仍属于本作品、未挂到不相容的其他活来源、目标 plan 不被另一个 live unit 占用，再显式恢复绑定。
4. unit 已被作者改挂到另一章／另一计划时，不能从新位置抢走；保留当前位置并返回“有规划未重新关联”的警告。同一个 unit 在多个快照中可以出现，恢复其中一个不授权覆盖另一个时期之后的显式改挂。
5. 老快照没有 planning_bindings：仍可恢复大纲；仍活着的 Chapter unit 可沿**恢复后的真实 Chapter.plan_id 关系**挂回。仅靠被删除 plan 的原整数 id 不重连其孤立 unit；保留待重新关联规划并提示。
6. 当前仍有章 unit 与快照期计划 unit 两者冲突时，保持章 unit 和孤立规划两份都存在，返回明确警告；不要自动合并文字。恢复链路若产品要求“要么全部自动保留原关联，要么不恢复”，可复用建章的 409 预检；这是可选更严格策略，默认保留数据＋说明未重连。

快照恢复必须把单次新的 planning revision 增量包含在同一事务中，即使新旧整数 id 完全一致。

## 旧数据、虚拟行与批量 CAS

**推荐一个项目级 `planning_revision`，而非只给已有 unit 加 revision。** 单行 revision 无法保护尚未持久化的来源行，也捕捉不到该章刚被重新挂接／删除后以同 id 重建。

- 迁移给 Project 增加非负 `planning_revision`，默认 0；它是运行时并发版本，放入 `PROJECT_RULE.exclude`，导入新项目从 0 开始。它不能随大纲快照／智能体撤销回退。
- GET 矩阵只读，返回统一 revision，以及 `unit_id: null, source: {kind: 'chapter'|'plan', id}` 的虚拟行；已关联章／plan 合并为一行且使用章 source。不要在 GET 中批量写空 unit，也不要要求只读管理员调用写接口初始化。
- 第一次写入时：获取父项目写锁与编辑权限→校验 expected_planning_revision→重新查询当前 source 与同项目引用→解析其实际 unit；没有才创建→执行整批修改→revision +1→一次 commit。
- `expected_planning_revision` 必须必填；所有目标、引用、数量限制与 CAS 完整校验后才写，任何失败整批回滚；重复 source、同章／plan 以两个别名重复提交需拒绝或规范去重，不能重复应用追加／移除。
- 现有 note CAS 提供返回 409 和保留本地草稿的模式：`note_service.py:70`–87；项目父行屏障 `project_lifecycle.py:50` 是跨 PostgreSQL／SQLite 的真实写锁。
- planning_revision 必须在**每个可能改变矩阵编辑语义的既有写入口**递增：章创建／删除／改号／标题／改纲，细纲创建／更新／排序／建章／重展开，大纲变更／恢复／覆盖，cast 修改，关联实体删除，新规划／节点／支线修改，智能体撤销相关行。正文纯文本编辑可不递增，因为它不改变手动规划；提及统计自行刷新。
- 这保证虚拟 `plan:7` 在读后删除并同 id 重建时，即使新内容碰巧相同，旧提交仍失败；不要用 created_at 或内容哈希冒充源代次，SQLite 同秒创建和相同文本重建会留下 ABA 空隙。
- GET 若分多次查询组成矩阵，在开始／结束读取 revision；变化则有界重试或返回可重试状态，避免把旧列表和新 revision 拼在一起。不要长时间持写锁做大型正文提及统计。
- 项目级 CAS 比逐行 CAS 更保守，会让不同章同时编辑也重试；首版成本可控，优先完整性。将来可拆成结构 revision＋unit revision，但不是本次必要基础。

## 备份版本与导入

- 推进 `ainovel.project` v2→v3：新增 unit／支线／节点／typed join 为核心表，不受 snapshots 可选项影响；旧备份 v2→v3 只初始化空规划表，不把现有角色／伏笔自动伪造成支线。
- `exporter.VERSION` 当前为 2（`project_transfer/exporter.py:33`），逐级迁移入口 `importer.py:253`；旧版本若携带冒充 v3 表，应仿 v1→v2 的处理显式清空（`importer.py:248`）。同步 APP_SCHEMA。
- 注册顺序：实体和 Chapter／ChapterPlan→PlanningUnit→Subplot／节点／joins→OutlineSnapshot。chapter_id／plan_id／pov 的 FK 由通用映射处理；detached_character_ids 进入 `json_refs`；纯标签／来源展示字段进入 `json_plain` 或普通列。
- 新 snapshot `planning_bindings.unit_id` 必须经 unit 映射，plan_id 经快照本地 pmap，不能简单通过当前计划表映射。现有 `remap.snapshot_data` 在 `remap.py:206` 为快照中不存在的源分配 synthetic id，235–240 行构造 pmap；从这个 pmap 取恢复后的 plan id。找不到 unit 就丢弃该 binding 并警告，不生成新 unit。
- 导入完成后统一检查 unit 的章／计划双向关联、全部引用同项目、同章／计划唯一性、节点归属和类型约束；不凭名字／编号修复不合法绑定。合法 orphan 允许导入且保持可见。
- 章节与计划内历史角色镜像差异兼容：v1/v2 导入不应为迁移强改角色安排，第一次相关写入才规范化；v3 导出应以权威 cast 规范化已建章 plan 的镜像，防止新备份继续制造歧义。

## 智能体撤销：实现前不可遗漏

`backend/app/services/agent/effects.py:40` 显式列出 EFFECT_TABLES，不自动采用全部备份表；它会快照、比对和还原 Chapter／Plan／cast，`effects.py:383` 用直接 SQL 恢复，绕过 service helper。若忽略此路径，撤销删章／改纲后会出现 unit 断联或复用了来源 ID，但 planning revision 未变。

- 相关规划表若会被已有智能体删除源／修改关系间接改变，必须登记到 effect capture；尤其 FK SET NULL 和 CASCADE 也是变化。
- 撤销预检必须检测被影响的 unit／节点是否在智能体操作后被作者改动；若变动则 409，不可用旧 effect 覆盖最新规划。
- `planning_revision` 不进入 project effect payload，撤销成功后单独递增。当前 `_RULES` 继承 PROJECT_RULE（`effects.py:71`），因此排除运行时计数器可以共享，但需要测试老 effects 的兼容。
- 原先不存在 unit 的 legacy effect 删除／重建来源，也需显式脱离或重连校验，不能仅依赖新 EFFECT_TABLES；采用“保留孤立规划＋冲突提示”的保守策略，优先不误挂。

## 必需验证与剩余技术风险

1. 未建章 unit 建章、复用已存在单章、双方都有元数据／同支线节点冲突，以及批次后部冲突时前部不能已落库。
2. 两种直接删源、重展开同时删 plan＋空章、向导覆盖／导入替换、章改纲解除 plan；所有节点保留且 orphan 不能进入任意章 AI。
3. 同整数 plan id 被删除重建的 stale 虚拟提交；同内容同创建秒数也要 409。
4. 新快照显式绑定、旧快照无绑定、快照 id 冲突重映射、作者已改挂后恢复、快照恢复不覆盖新规划文本。
5. cast 三个既有入口（章、细纲、建章）及矩阵入口一致；删章退回计划时角色保留；没有第三份 live 角色列表。
6. v1/v2兼容、v3完整往返含 orphan／全部设定类型／可选快照，错误跨项目或重复绑定整笔拒绝。
7. 智能体撤销源对象以及后续作者已修改规划的冲突保护。

**最大风险是遗漏结构变更入口，不是表设计本身。** 特别是 wizard_service 批量 SQL、outline_snapshot_service 重插原 id、agent.effects 直接 SQL 都绕开普通 CRUD。建议实现先集中 lifecycle helper 和覆盖测试，再接 UI，避免将“FK SET NULL 未报错”误当作完整保留。

## Caveats / Not Found

- 仅设计研究，未改代码、未运行测试。这里给的是推荐方案，需由主会话收敛进入正式 design.md。
- 项目级 CAS 需要核对所有 AI／工具写入是否已持父项目锁；当前普通人工路径已有屏障，不能推断所有后台路径一致。
- 本报告推荐明确的建章冲突预检与人工合并；若决定让首版自动合并，必须先明确同章同支线能否有多个节点和冲突 POV 选择，不能由实现者猜测。
