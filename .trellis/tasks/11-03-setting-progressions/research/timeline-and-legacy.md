# Research: 设定时间线、历史还原与旧写入兼容

- Query: 核实关系、职业、角色／世界观及章节分析的写入、撤销、删除和存量还原能力；提出最小可执行时间线方案。
- Scope: internal；仅研究，不修改产品代码。不覆盖 UI、生成入口总清单、备份注册表。
- Date: 2026-11-03（Asia/Shanghai）
- 状态：下文“建议”均为技术方案建议；用户已确认章节级首版范围，但未单独选择旧 API 歧义处理与存量迁移策略。

## Findings

### 1. 结论

1. **不能直接把现有历史按章节排序当作剧情状态重放。** 现有 `before` 是操作发生时的全局当前值，不是该章之前的值；手工写入不检查章序。类型／方向／职业阶段部分只有名称；无章节修改与删除还产生不可恢复的缺口。证据：`backend/app/services/relationship_service.py:260`、`:296`、`:314`；`backend/app/services/career_service.py:379`、`:387`、`:395`、`:417`。
2. **应区分基础设定、故事变化和操作审计。** 原有历史可以继续展示审计，新时间线需要独立、稳定的目标身份、可验证的初始值和绝对变化值。否则乱序补录与撤销会把未来状态带回过去。当前通用角色／世界观更新只是逐字段 `setattr`，并无基线历史：`backend/app/services/story_service.py:115`。
3. **父会话提出的章首／章末方案可成立。** 手工记录“本章起”是 `(N,start)`；分析结果是 `(N,end)`。写第 N 章时包括 N 的 start、排除 N 的 end；写更晚章时包含 N 的 end。需存 boundary，不能把章末简单存成 `N+1`。现有分析只存源章节 N，没有章内边界：`backend/app/models/entities.py:663`、`:695`。
4. **分析早章应更新该章历史，不直接覆盖最新物化状态。** 现有 `_Writer.plan` 会把早章同字段直接标记 skipped，这只保护当前值，不能支持补录中间状态；其无变化判断也比较全局当前值，必须改为目标章位置的有效值。证据：`backend/app/services/writeback_service.py:169`、`:215`、`:263`；现有测试明确固定旧行为：`backend/tests/test_writeback.py:223`。
5. **撤销应停用原变化并重算，不再补一条同章反向业务事件。** 审计仍保留；后续绝对 replace 保留其作者输入值，后续 append 按新前值重算。现有撤销是当前值必须等于 after、再写 before；关系／职业还补同章反向历史：`backend/app/services/writeback_service.py:483`、`:500`、`:529`。
6. **无章节 PATCH 的兼容存在真实语义冲突。** “改当前值”和“改初始值”在已有后续 replace 时不同，不能静默同时满足。完整表单中与当前值相同的字段首先应忽略，避免把最新值写回基线；真正变化且有时间线的字段需显式意图，或在旧 API 给出可操作的冲突。来源：`backend/app/services/story_service.py:127`、`backend/app/schemas/career.py:100`。

### 2. 文件与证据入口

| 文件 | 职责／关键位置 |
| --- | --- |
| `backend/app/services/story_service.py:71` | 创建角色并 flush，供组合创建／导入复用。 |
| `backend/app/services/story_service.py:99` | 通用角色／世界观创建、更新、删除；无时间语义或手工字段历史。 |
| `backend/app/services/relationship_service.py:244` | 关系唯一业务写入口，校验角色／类型／唯一性，更新当前表并追加展示型历史。 |
| `backend/app/services/relationship_service.py:502` | 手工更新传 chapter_no，但该值只流向历史。 |
| `backend/app/services/relationship_service.py:522` | 关系硬删除，历史随外键级联。 |
| `backend/app/services/career_service.py:166` | 阶段原位改名／重排，删除只检查当前挂接。 |
| `backend/app/services/career_service.py:353` | 新挂、换阶、备注、主副变化、卸下唯一业务入口。 |
| `backend/app/services/career_service.py:564` | 手工挂接／更新可带章号；卸下入口不接章号。 |
| `backend/app/services/organization_service.py:278` | 成员快照、写入和上下文查询；当前快照未包含 note。 |
| `backend/app/services/writeback_service.py:117` | 查询同字段已应用分析的最大章号。 |
| `backend/app/services/writeback_service.py:160` | 比较全局值、跳过早章、待确认分流、逐项 SAVEPOINT。 |
| `backend/app/services/writeback_service.py:483` | 撤销；当前值比对、反向写入或删除新关系。 |
| `backend/app/services/writeback_service.py:572` | 确认待确认条目；复验后续章和全局当前值。 |
| `backend/app/models/entities.py:359`、`:441`、`:684` | 关系历史、职业历史、分析变化的实际字段与级联关系。 |
| `backend/app/ai/prompts/templates/analysis.chapter.user.j2:43` | 模型只报告正文发生的变化，输出变化后的绝对值。 |

### 3. 当前写入／删除／撤销行为

#### 3.1 角色与世界观

- `create_owned` 直接创建当前行；`update_owned` 对 `exclude_unset=True` 且非 null 的字段逐项赋值；`delete_owned` 硬删除。没有手工编辑的章节、旧值或意图记录（`story_service.py:99`、`:115`、`:135`）。
- 角色分析只允许 `current_state` / `life_status`，`set_character_field` 读取全局旧值后覆盖，不携带章号；时间线信息仅由外层 `AnalysisChange` 记录（`writeback_service.py:57`、`:71`、`:199`）。
- 因此，仅有当前角色／世界观行不能恢复旧简介、外貌、状态等。某条 `AnalysisChange.before` 可以证明该次分析写入前的操作状态，但不能证明它自故事起始一直有效。

#### 3.2 人物关系

- 当前关系按无方向角色对唯一，`a_id/b_id/type_id` 描述方向；改方向的 PATCH 只能在原两人中互换（`relationship_service.py:505`）。
- `apply_change` 先按当前行合并请求，仅把 `chapter_no` 写进历史，未按故事时间排序、也未限制较早章修改当前值（`:260`、`:314`、`:325`）。
- 历史 `type` 是角色称呼标签，`direction` 是“姓名→姓名”；不是 type_id / a_id / b_id。改名、同名、类型删除后无法总是无歧义反查身份（`:296`）。
- 标量 intimacy/status/description 保存 `[旧,新]`，新建全部旧值为 null（`:308`）；无变化不记录，所以“同样的值在另一章再次确立”并无现成事件（`:311`）。
- 删除关系直接删除当前行，`RelationshipHistory.relationship_id` CASCADE，所以被作者删除的旧关系没有可供恢复的存量历史（`relationship_service.py:522`；`models/entities.py:365`）。
- 新建关系的分析回写经用户确认后补入真实 `a_id/b_id/type_id`，这一类 `AnalysisChange.after` 比普通关系历史更完整；撤销该新建关系则硬删除关系（`writeback_service.py:591`、`:614`、`:503`）。

#### 3.3 职业挂接／阶段

- 当前挂接唯一键是 `(character_id,career_id)`；卸下会物理删除 `CharacterCareer`，再次挂接可能得到新的 assignment id。历史仍按角色＋职业保存，故新时间线不得把 assignment id 当唯一稳定主体（`models/entities.py:421`；`career_service.py:379`、`:395`）。
- 新挂历史有 role/stage/note 的初始值，阶段只有“第 N 阶·名称”；更换阶段也是标签对（`career_service.py:387`、`:410`、`:417`）。
- 卸下历史只有 `removed:[career.name,null]`，不含卸下前的阶段、角色类别、备注；HTTP 卸下不收 chapter_no，因此这次发生在故事哪里未知（`:395`、`:604`）。
- 职业类别改动会同步所有当前持有人的 role 并写无章号手工历史；职业和阶段定义可改名／重排。阶段删除只查当前持有人，没有历史引用保护（`:269`、`:298`、`:166`）。
- 删除职业会级联删除阶段、挂接和挂接历史，无法事后重建（`:303`；`models/entities.py:453`）。
- 主职业 1、副职业 3 的约束只检查当前挂接。新时间线若允许跨章挂接与卸下，需在受影响时间区间检查这些约束，不能仅检查最新投影（`career_service.py:340`）。

#### 3.4 分析回写与撤销

- `AnalysisChange` 有 project_id、chapter_no、field_key、target、before、after、状态和应用／处理时间；其源分析保留正文 hash。没有关联到关系／职业历史的 history_id（`models/entities.py:651`、`:684`）。
- 当前角色、关系和职业的 before 都取全局当前值。先分析第 20 章再补第 5 章时，该 before 可能已经是第 20 章值（`writeback_service.py:215`、`:263`、`:294`）。
- `_Writer.plan` 先判断 before==after，再判断更晚 applied，随后 pending／SAVEPOINT。因此直接接入新事件时必须先换 before 来源；否则“第 5 章 after 恰好等于第 20 章当前值”的有效变化会被省略（`:169`）。
- 新关系、存活状态待确认，职业阶段只接受当前已挂职业；确认还会再次拒绝较早章，并比较全局 before。需改成目标时间线及修订号校验，保留重大变化由用户确认的政策（`:59`、`:282`、`:572`）。
- 当前撤销以 current==after 为门槛，写回 before，最后把 AnalysisChange 改为 reverted；后续把字段改回相同值的 ABA 情形不会被识别。新事件模型宜根据原 event id＋revision 判断是否被手工改动，而不是只比文本相等（`:483`）。
- 关系／职业的反向历史 source 仍为 analysis，章号仍是原章号，只有 note 表示撤销。不能把“撤销第 N 章分析”备注解析作为新模型的撤销事实来源（`:488`、`:500`、`:529`）。
- 分析最终保存使用 job 中的章号／hash；保存时只检查章节是否仍存在，没有在这里重比正文 hash 或当前章号。新时间线写入必须在提交边界核实源章节与正文修订，避免运行中的分析把已改稿的旧结论激活（`analysis_service.py:784`、`:794`、`:814`）。

#### 3.5 角色组织归属是必要联动

- 角色卡会展示所属组织的职位／状态，若只回溯职业与关系，这条读取仍可能泄露未来。首版最低限度跟踪成员目标 `(character_id,organization_id)` 的 `exists/position/status/note`，不扩大为组织树、权力或组织详情的时间化。
- `OrganizationMember` 已有 position/status/note，唯一键为组织＋角色（`models/entities.py:286`）。手工更新直接写字段、删除直接删行（`organization_service.py:247`、`:260`）。
- 分析 `membership_snapshot` 及 `apply_membership` 只处理 position/status，并以 None 表示移除，未覆盖 note（`:278`、`:290`）。新规范应把 note 纳入统一快照，同时保留分析未提供 note 时不改其值的 PATCH 语义。
- `memberships_of` 当前直接遍历所有成员行（`:322`），历史读取需要通过新主体投影，而非仅依赖还存在的行。

### 4. 最小完整数据形状建议

以下为概念契约，可按项目迁移／FK 约定落成类型化表；不建议把这些内容都塞进已有展示型 history.changes。

#### 4.1 时间线主体与初始值

```text
SettingTimeline
  id, project_id, subject_kind, revision
  subject identity:
    character  -> character_id
    world      -> world_setting_id
    relation   -> pair_low_character_id + pair_high_character_id
    career     -> character_id + career_id（不依赖当前挂接行）
    membership -> character_id + organization_id（不依赖当前成员行）
  baseline: TypedSnapshot
  legacy_origin / completeness（至少能区分人工确认、迁移现值与可靠历史还原）

TypedSnapshot:
  character -> {appearance, personality, background, profile,
                current_state, life_status, custom_fields:{field_id:text}}
  world     -> {content, custom_fields:{field_id:text}}
  relation  -> {exists, identity:{a_id,b_id,type_id,role_a,role_b},
                intimacy,status,description}
  career    -> {exists, role, stage_id, stage_label, note}
  membership -> {exists, position, status, note}
```

- 字段白名单应在单一 service/schema 中定义；具体哪些已有文本纳入首版由主设计最终列明。名称、别名、分类等是否时间化不应由任意 JSON 允许字段隐式扩大。
- 关系 type/direction 合为 identity 原子组，防止换方向时只更新一半；稳定 ID 是引用事实，展示标签快照用于历史渲染，不用名称重新猜 ID。类型／阶段删除需明确历史引用策略：最简单的可靠方案是存在有效历史引用时禁止删除，先迁移或清除相关变化；否则仅存 ID 的历史会断裂。
- career/membership.exists=false 必须仍能保留时间线，使第 20 章卸下／退出后查看第 5 章还能知道过去的归属；读取方必须查询主体／事件，而非只枚举当前 CharacterCareer / OrganizationMember。

#### 4.2 变化事件

```text
SettingChange
  id, timeline_id
  chapter_no >= 1
  boundary: start | end
  order: stable integer（同主体、同章、同边界的确定顺序）
  field_key: BuiltinField | custom:<stable field id>
  operation: replace | append
  value: validated typed value
  source: manual | analysis | legacy_import
  source_change_id?（关联 AnalysisChange；不以备注推断）
  source_content_hash? / revision
  active / superseded_by? / reverted_at?
```

- 文本字段支持 append/replace；存活状态、亲密度等标量和身份／存在性只允许 replace。
- 新建／解除关系或职业应以事务内的原子组记录完整初始／终止状态，不能把“缺字段”当“存在=false”。普通更新只保存作者实际改变的字段，避免回填早章时把未改字段的未来值打包到过去。
- 章末保存 `(N,end)`；“下一章起”是这条变化的效果说明。不要把它改为 `(N+1,start)` 后丢掉原章关联。
- 有效结果：`baseline` 加上按 `(chapter_no,boundary,order,id)` 排序的 active 变化。章首视图包含 `(N,start)` 所有记录，排除 `(N,end)`；章末视图包含二者。
- 同章多条 append 要有稳定顺序，编辑文本不自动挪动顺序。替换覆盖该字段之前的累计值；后续追加继续作用于替换结果。
- 当前业务表可保留为 latest 投影以兼容现有列表，但只能由新 service 在同一事务内同步。as-of 读取返回 DTO，不能把历史值写进 ORM 当前实体，以免查询造成 flush。

### 5. 写入、分析与撤销的最小流程

1. 在现有项目写屏障下加载主体、基线、相关事件和 revision；所有变更与 latest 投影同事务提交。既有项目行锁与不 commit 内层业务原语可以复用（`story_service.py:207`；`career_service.py:369`；`.trellis/spec/backend/database-guidelines.md:101`）。
2. 显式基线编辑只更新 baseline 的指定字段；显式 progression 写入按目标章前值校验。对关系存在性、身份和职业上限检查全部受影响区间，不只检查当前行。
3. 分析读取目标章开始时的状态，给出章末绝对 after。早章分析可以生成该章事件，但 latest 投影仍由更晚章事件决定，替代旧的“跳过早章同字段”规则。
4. before/after 相等判断针对该章目标位置，不能针对全局最新值。未解析目标、跨项目引用和模型校验失败继续按现有 skipped／failed 规则处理，保留逐项 SAVEPOINT。
5. source_change_id 关联具体生成事件。重复分析同一章应明确“同章同字段自动事件替代”规则，避免每次重跑都累加记录；幂等不能只比较当前值。
6. 撤销分析条目停用其关联的原事件，再重算后续及 latest。原事件被手工编辑／替换后，用 event revision 拒绝无意撤销手工成果；后续独立绝对事件不构成禁止撤销的理由。
7. 示例：基线 A → 第 5 章 replace B → 第 20 章 replace C。撤销第 5 章后，5–19 章为 A，20 章仍 C；不能添加“第 5 章 replace A”的补偿事件而留下旧 B 与顺序歧义。
8. UI 的数据删除与剧情变化必须分开：现有关系 DELETE 代表删除资料、历史随之清理；“关系破裂／职业卸下／成员退出”应是可回溯事件。挂接卸下新增带故事位置的操作，旧无章 DELETE 不能偷偷猜章节。

#### 5.1 exists=false 与当前表、旧审计共存的最小方案

**推荐采用稳定时间线主体＋少量当前投影适配，不建立通用事件框架。** timeline 的归属 FK 指向项目及角色／职业／组织等稳定资料，不指向可删除的 CharacterCareer / OrganizationMember，也不依赖当前 Relationship.id 识别人对。

| 对象 | latest.exists=false | 为什么不会丢历史 | 恢复 exists=true |
| --- | --- | --- | --- |
| 职业挂接 | 可物理删除当前 CharacterCareer 行。 | 现有 CareerHistory FK 在 character/career 上，新 timeline 也以该二元组为主体，不会随 assignment 行级联（`models/entities.py:449`、`:453`）。 | 按稳定二元组重新创建当前行，业务 ID 可改变，历史事件不变。 |
| 组织成员 | 可物理删除当前 OrganizationMember 行。 | 新 timeline 以 character/organization 为主体；目前没有 FK 到该成员行的专属历史。 | 按稳定二元组重新创建当前行，as-of 不依赖新旧 member id。 |
| 人物关系 | **保留 Relationship 作为载体，增加 `is_present=false` 投影标记。** | `RelationshipHistory` 仍 FK 到关系载体，因不删除行而保留原审计；新 timeline 则以稳定人对定位。 | 复用隐藏载体及原关系 ID，设置 is_present=true；不能另建一条撞 pair 唯一键。 |

- 只有作者明确“删除整份关系资料”才删除 timeline、载体及旧历史；删除某次创建事件／撤销分析创建不属于资料删除，应重算为 absent。该区分必须进入 API 契约，不能继续共用硬 DELETE。
- `is_present` 不等于关系 status=broken/past：破裂仍是一条存在的关系，照常可展示；absent 才从当前界面／图谱移除。
- 关系当前查询应统一使用同一 `present` 条件：`list_relationships`（`relationship_service.py:442`）、`relations_among`（`:581`）、`graph` 关系查询（`:615`）以及普通 `find_pair` / 获取当前关系／AI 缺失关系判断。内部载体查找、历史页、复建冲突判断必须显式允许 absent，不得重复实现各自的过滤语义。
- 关系新建服务先按人对查包括 absent 的载体：present 则沿用原 409；absent 则复用载体并登记新创建事件。`apply_change` 的唯一性检查需要与这条规则一致。
- 职业与成员当前查询可以继续依赖 latest 物化行存在性，因此现有 list/count/graph 的大部分语义无需新标记；历史查询必须使用统一 `resolve_careers_at` / `resolve_memberships_at` 等投影入口，从 canonical 主体读取，不能以当前 assignment/member 集合为候选。
- 当前投影同步可复用低层校验／赋值，但不能再调用带“写 timeline”的外层 service 形成递归。保持“事件变更 → reducer → materialize”单向流程，并在同一项目锁／事务完成。
- 替代方案是把 RelationshipHistory 的 FK 从关系行迁到 timeline 主体，然后所有当前表都可物理删除。该方案结构更一致，但涉及旧历史迁移、旧 URL 和 AnalysisChange 引用兼容；本期没有必要为对称性扩大改造。

### 6. 无章节旧 API 的兼容选项

| 方案 | 行为 | 影响 |
| --- | --- | --- |
| A. 显式意图，歧义拒绝（推荐技术选项） | 先忽略与当前投影相同的字段；无时间线的实际改动沿用基础编辑；有时间线的实际改动要求 baseline 或明确章锚，否则 409 并指向新操作。 | 不猜剧情时间、不覆盖后续变化；旧第三方调用者需升级，新前端与智能体需同步。 |
| B. 所有无章 PATCH 都改基线 | 仍忽略相同字段，实际变化写 baseline。 | API 形式兼容，但有后续 replace 时最新值不变，原“保存当前状态”的语义变化；必须让界面展示的是基础值并明确说明。 |
| C. 无章 PATCH 改最后一条事件／推算当前章 | 表面保持保存后当前值改变。 | 没有可靠故事位置，可能改写旧分析或计划未来事件，破坏 source 链；不推荐。 |

完整表单防污染例子：基础境界“练气”，第 20 章变化“金丹”，旧表单修改人物性格同时原样提交 current_state="金丹"。若逐字段赋值基线，练气被覆盖，回溯立即失效；应先比较当前值，current_state 相同则完全跳过。

“基础设定”专用接口比较的是 baseline，而不是当前投影。否则作者明确把基础值改成恰好等于最新值时，也会被错误当成无变化。

### 7. 存量迁移策略

| 数据来源 | 能证明的内容 | 不能无条件证明的内容 |
| --- | --- | --- |
| 当前角色／世界观行 | 安装功能时的值 | 剧情初始值、任意早章值、未留痕的手工编辑 |
| 关系标量历史 | 每次操作前后标量及部分章号 | 所有章节的顺序语义、已删关系、无章修改发生位置 |
| 关系类型／方向历史 | 当时展示标签与姓名方向 | 稳定 type_id、同名／改名后的端点映射 |
| 职业历史 | 新挂的展示阶段／角色类别／备注、阶段变化标签 | 无章卸下位置、已删除职业／阶段、完整卸下快照 |
| AnalysisChange applied | 某次被应用的绝对值、目标 ID、章号；职业 stage_id；已确认新关系的身份 ID | 该 before 是否是故事初始值、未记录手工修改、所有早章的真实状态 |
| AnalysisChange skipped/pending/rejected/failed | 一条未被成功采用的分析建议 | 可直接生效的作者认可事实 |

**策略 M1：保留现值作为迁移基线，不自动重放旧日志。** 最小、确定、不改现有作品数据；旧日志继续可看，新增变化从此完整记录。必须显式标识旧基线未经故事初始校准，允许作者修订初始值和补历史；不能声称旧作品安装后已实现无遗漏回溯。该策略是否符合本期兼容预期需要主会话确认。

**策略 M2：在 M1 上增加严格可证明的旧链导入。** 仅对逐字段可验证、引用可解析、无未定时间编辑、尾值等于当前值的链自动导入；其余保留 M1 并报告需校准。至少需要：

- 按操作时间先验证 `before → after` 连续性与当前尾值，再按故事位置形成新事件；不能先按 chapter_no 排序再把“最早 before”当基线。
- 拒绝把 source=analysis 的关系／职业展示历史与同源 AnalysisChange 重复导入。旧结构没有直接 history_id，不能仅靠 note 去重或判断撤销；应选择一个可证明的权威来源。
- `reverted` 不激活，pending/rejected/skipped/failed 不自动转 active；原来“更晚章节优先”而 skipped 的早章结论也未获应用授权，需要重新分析或人工采用。
- 需要通过稳定 ID 或可靠唯一映射还原身份；无法还原时只保留旧日志展示，不猜测名称到 ID。
- 对存量缺少起点的角色文本、硬删除资料、无章卸下，不承诺恢复。历史标签可以显示，但不能假称结构化还原成功。

**不推荐策略：用当前值逆应用全部 `chapter_no > N` 的 before。** 有无章手工修改、乱序写入、旧撤销、重复分析或缺失日志时会得到错误状态，甚至把未来值作为初始值。

### 8. 需要主会话明确的真实产品决策

1. **无章编辑语义**：选择上表 A 或 B；单纯代码证据无法决定作者是修改初始设定还是修订最新状态。
2. **旧作品覆盖承诺**：本期接受 M1 的明确旧基线提示与人工校准，还是必须增加 M2 的可靠链导入；不应承诺无法恢复的数据。
3. **重新分析后旧事件的处理**：同章新分析没有再次报告某变化时，是停用旧自动事件、还是保留并提示复核；已经手工确认的死亡／新关系是否需要再次确认。现有代码只追加 AnalysisChange，并没有这项产品约定（`analysis_service.py:790`；`writeback_service.py:170`）。
4. **同边界人工与分析冲突**：建议人工明确维护的事件优先，重跑分析替代自己的旧事件但不覆盖手工事件；需在最终契约中写死来源优先／顺序，而不是偶然依赖提交时刻。

章首／章末双边界本身不要求引入独立场景，也能满足“从本章起／从下一章起”的已批准章节级范围。

### 9. 必须覆盖的验证场景（未运行测试）

- 第 20 章先晋升、再补录第 5 章，查看 5/10/20/21 章正确；特别验证早章 after==最新值仍能落事件。
- 第 20 章分析章末晋升：重写 20 章的章前设定仍旧境界，21 章使用新境界；同章手工 start 事件可参与 20 章。
- 先 append、再 replace、再 append；同章同字段稳定顺序；编辑／删除中间事件后重算。
- 角色表单提交最新值未变时不污染 baseline；明确修改 baseline 为等于最新值的文本仍可保存。
- 关系在第 10 章建立，5 章不存在；类型／方向修改原子、同名角色不靠名称定位；剧情破裂与删除资料区分。
- 职业第 10 章挂上、20 章卸下、30 章再挂，15/25/35 章分别有效／无／有效；current 行已删仍可查旧挂接；历史区间主副上限。
- 成员职位／状态／备注不泄露后期变化；20 章退出后回看 5 章仍显示原归属；删除／撤销创建事件后当前 list/graph 不显示幽灵关系，旧 RelationshipHistory 仍可通过时间线查看，复建同人对无唯一键冲突。
- 撤销第 5 章事件且保留第 20 章绝对事件；原事件被手工编辑后撤销冲突；ABA 不绕过 revision。
- 自动分析早章不覆盖后期 latest；同章重复分析幂等；正文或章序在分析运行期间变化的提交处理。
- M1 保留原当前值；M2 如采用，覆盖标签无法解析、无章编辑、被撤销、已删除引用、重复审计来源、非连续 before/after。
- 既有 `test_writeback.py:223`、`:339` 的“拒绝早章”断言需要随新契约调整，同时保留“不覆盖晚章当前投影”的实质保障。

## Related Specs

- `.trellis/spec/backend/story-entities.md`：关系唯一入口、职业历史、分析回写与撤销。
- `.trellis/spec/backend/database-guidelines.md`：事务、父项目写锁、SQLite／PostgreSQL 迁移边界。
- `.trellis/spec/backend/rbac-quotas.md`：人工设定维护的既有授权屏障。
- `.trellis/spec/guides/cross-layer-thinking-guide.md`：共享事件类型、规范化和投影的唯一所有者。

## External References

本研究只核实本仓库的数据和写入语义，未依赖第三方库用法或外部资料。产品参照文档由主会话另行核实。

## Caveats / Not Found

- 没有访问用户实际数据库；不能估计存量中可可靠回填的比例。
- 没有运行测试或修改实现；测试位置用于识别既有契约，并非功能已通过的证据。
- 已存在的日志并非完整事件源，丢失信息无法通过迁移算法补造。
- UI、完整生成入口、备份／智能体效果注册表由其他研究负责；这里的字段／关联方案须由主设计与这些边界共同收敛。
