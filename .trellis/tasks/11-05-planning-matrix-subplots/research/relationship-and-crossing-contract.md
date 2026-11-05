# Research: 人物关系关联与支线交汇的 AI 契约

- Query: “全部现有设定类型”是否应含人物关系，以及交汇事件进入 AI 的当前设计是否完整？
- Scope: internal
- Date: 2026-11-05

## Findings

### 1. 人物关系应作为第五类可选设定

结论：应纳入 `relationship`。它已有独立页面、实体、历史与图谱，用户确认“全部现有设定类型”时，不能只覆盖角色、world、组织、职业四类。

- `frontend/src/components/StudioNavigation.tsx:37` 有“关系”导航；`frontend/src/router.tsx:111` 有独立页面路由。
- `frontend/src/pages/project/RelationshipsPanel.tsx:53` 定义关系列表、筛选、维护、历史、类型管理和关系图。
- `backend/app/models/entities.py:332` 是独立 `Relationship`，并按规范化角色对唯一；`:359` 的 `is_present` 区分当前可见与历史载体。
- 组织成员、角色职业挂接是附属状态，身份分别为角色＋组织、角色＋职业（`backend/app/schemas/progression.py:60`—`:61`），无须作为另外两个手动选择类型；它们继续随相关角色的有效设定注入。
- 原设计 `PlanningUnitWorld/Organization/Career`、API reference kind、entries、前端筛选、备份和验收均需补 relationship；正文自动提及仍不增加人物关系类型。

### 2. 推荐引用稳定关系时间线，而非仅当前载体

最小可靠方案：新增 `PlanningUnitRelationship(unit_id, timeline_id)`，两列唯一，分别外键到规划单元与 `setting_timelines`，CASCADE；服务额外校验时间线 `subject_kind == relationship`、同作品、两端角色同作品。对外使用类型化 `kind: relationship`，明确其目标为 `timeline_id`，避免与旧 API 的 `relationship_id` 混用。

依据：

- `backend/app/models/progressions.py:34`、`:59` 用规范化人物对建立稳定唯一主体；该身份不依赖当前关系载体。
- `relationship_service.py:228` 提供含隐藏载体的 `find_pair_record`；普通列表在 `:730` 过滤 `is_present=True`，直接复用列表会漏掉旧章节仍有效的关系。
- `relationship_progression_service.py:141` 支持以 `exists=False` 创建稳定时间线；`:273`—`:280` 显示：不存在且尚无载体时可以只有时间线，没有 `Relationship` 行。只使用 `relationship_id` 无法覆盖这种合法关系档案。
- `backend/alembic/versions/0031_setting_progressions.py:126`、`:155` 为旧关系生成基线；正常新建也使用稳定时间线。因此无需新增第二套关系身份。

推荐列表 DTO 同时提供 `timeline_id`、可空 `relationship_id`、`character_ids`、两端姓名、当前是否存在；目标为已建章时附该章的 `effective_exists` 和有效关系描述。未建章只显示主体身份与“建章后按章初求值”，不能以当前关系描述冒充历史值。可按两端姓名和关系类型搜索，但关联事实不按名字匹配。

当前/隐藏均可选择，隐藏并非删除；关系尚未在目标章建立时，保留手动关联并明确显示“本章尚无有效关系”。不要阻止作者提前规划，也不能把后续关系类型、亲密度或关系描述提前注入。

### 3. 有效历史、两端角色与去重

推荐读取流程：

1. 批量加载同作品选中的关系时间线以及两端角色；不调用创建／物化／关系写服务。
2. 在 `StoryPosition(chapter_id, chapter_no, boundary="start")` 统一调用 `resolve_timelines`，读取 `exists`、`identity`（含有方向 a/b/type）、`intimacy`、`status`、`description`。
3. `exists=True` 时按该章的身份和类型生成说明；`exists=False` 时至多输出“人物 A／B 在本章开始时尚无有效关系记录”的中性提示，不读取最新 `Relationship.description/type_id` 补充。
4. 名字和必要的人物资料按同章有效设定解析。为匹配当前代码，可把两端人物加入只读的“参考角色”集合并去重，不写进手动出场列表，不宣称两人一定同场。
5. 与既有自动关系按稳定 timeline id／规范化人物对去重；显式关系优先进入受保护规划块，其余关系仍保留既有裁剪。加入两端不能递归把全书社交关系都扩展出来，只计算当前参考角色集合之间的关系。

代码依据：`context_builder.py:616`—`:654` 已从时间线求值而非最新载体输出关系，但只保留两端都在 `involved` 的关系（`:648`）；`resolve_effective_cast` 在 `:530` 同样过滤两端集合。所以只传 relationship ID 而不补参考角色会丢失手动关系。现有类型校验允许全局预置或本项目类型（`:633`），该边界不能放宽。

推荐在投影结果保留角色“显式出场／POV／关系参考”来源标记。关系参考角色可以提供有效档案，但固定提示词应明确这些人物是关系理解背景，并非要求自动安排其出场。

### 4. 隐藏、删除、备份与版本

- 结束关系只令当前载体不可见；规划关联保持，早期章仍可求值。不要把 `is_present=False` 当作失效 FK 清掉。
- 真正删除整份关系资料会同时删除时间线和载体，见 `relationship_service.py:822`—`:844`；规划关联经 timeline FK 清理，项目 planning_revision 递增，防止旧界面重新提交已删关系。
- 两端角色删除会级联删时间线（`models/progressions.py:67`—`:71`），关联随之清理；跨作品或已删除引用批量写入失败。整数 ID 可能被重用，旧草稿依靠规划 revision 与现有来源身份校验拒绝，不能因新对象同 ID 自动续接。
- 导出新增 `PlanningUnitRelationship` 到 project transfer RULES；`timeline_id -> setting_timelines` 必须真实 remap，不映射到 relationships。已有时间线位于 `registry.py:312`，baseline 的关系 type ID 通过 setting_payload remap（`:318`）。新关联表应在时间线与规划单元之后导入。
- 当前 relationships（`registry.py:179`）和关系历史（`:188`）继续独立完整备份；不要为了支持规划省掉隐藏载体或其历史。v3 往返覆盖隐藏关系、仅有时间线的关系、两端/类型 ID 重映射以及关系引用不能指向非 relationship 时间线。
- 智能体效果的直接 SQL 清理／恢复需包含新关联表；时间线变更仅改变有效资料时不必改变规划绑定版本，真正删除引用则必须使旧规划版本失效。

### 5. 当前交汇设计审阅

审阅对象：本次读取的 `design.md`，状态为最终方案候选，已有 `SubplotCrossing` 与参与集合，且用户已确认显式交汇。

现设计已正确覆盖：目标单元挂载、无普通节点也生效、事件按 ID 只注入一次、完整参与名单、作者概览不注入、局部操作约束、同章节点/事件预算公平、源身份变化、旧模板锁定兜底。

建议补充以下易遗漏契约：

1. **交汇也必须标为计划目标**。当前 AI 章节明确“节点是本章目标”，应同时写“交汇事件是本章规划，不是既成事实”。计划事件即使落在已写章节，也不能仅凭有正文就断言已经发生。
2. **AI 读取与 UI 筛选完全分离**。交汇的完整参与集合来自服务端同项目查询，不能因为矩阵只显示 2 条支线，就把 3 方交汇缩成 2 方；也不能为了补齐参与上下文拉入其他章节的节点或支线总述。
3. **事件绑定与参与验证共同完成**。同章交汇引用的支线必须同项目；没有普通节点时仍注入事件，不能通过“从目标章 nodes 获取 subplot_ids 后查 crossing”的路径意外漏掉。节点与事件各取一次，再按稳定 ID 排序组合。
4. **身份与日志**。生成规划快照／预览至少包含实际采用的 node IDs、crossing IDs 及 crossing participant IDs；名称可改，只有 unit revision 不能直接解释哪一个共享事件被注入。实际 messages 继续是最终文本证据。
5. **局部/续写语义**。事件描述和普通节点相同，只作当前选区的一致性背景，不强制在任意小片段完成整场交汇；自动续写复用首次事件快照，不随 UI 中途改参与者改变当轮故事。
6. **无普通节点但有交汇也属于“有新增规划”**。空块/预算/旧提示词 parity 的条件必须包含 crossing，不能仅检查 POV 或 nodes。

### 6. 必须追加的验收用例

- 关系在第 10 章建立、第 20 章结束：关联第 5、15、25 章分别为未建立／有效／不存在；当前隐藏状态不影响第 15 章读取历史。
- 第 15 章有效关系类型与当前类型不同，实际消息只含历史类型、状态及描述；未建立时不泄露未来关系身份。
- 无 POV／无手动出场，只关联一段人物关系仍能得到有效关系资料；两端成为参考角色，不改变 ChapterCharacter 或 plan.character_ids。
- 关系作为手动关联及既有角色间关系同时出现时只有一份；同一整数对应其他 kind 不互相去重。
- 真正删关系或删一端角色清理关联并让旧 revision 失败；隐藏不清理。关联仅有时间线、尚无载体的关系仍可备份恢复。
- 三条支线的交汇没有普通节点，生成和重写仍注入一次完整事件；矩阵筛选隐藏其中一条也不改变 AI 消息中的参与集合。
- 同章普通节点＋交汇同时存在、事件同名但不同 ID 时不误去重；另一个章节的交汇或后续节点不泄露。
- 只有交汇的章节不走“无规划”分支；长事件与多个节点预算可解释；旧模板、自动续写、批量与局部操作都覆盖事件。

## Related Specs / Files

- `.trellis/spec/backend/setting-progressions.md`：关系稳定人物对与章初有效值。
- `.trellis/spec/backend/story-entities.md`：关系语义、人工关系维护、角色选角。
- `.trellis/spec/backend/project-transfer.md`：表规则、外键及 JSON remap。
- `research/ai-integration-design.md`：模板、预算与多入口接入。
- `design.md`：当前候选方案；本报告只指出补充项，不直接修改其内容。

## Caveats / Not Found

- 未修改产品代码，未运行测试；建议基于现有代码与候选设计。
- 若选择坚持 `relationship_id` 作为唯一引用，需明确补齐“只有稳定时间线、尚无当前载体”的情形；不能用普通当前关系列表隐式缩小“全部关系档案”。
- 当前设计未显式声明关系是第五类；以上补充完成前，不宜宣称全类型范围已经收敛。
