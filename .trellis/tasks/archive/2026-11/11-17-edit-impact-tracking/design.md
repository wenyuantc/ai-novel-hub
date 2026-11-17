# 技术设计：修改影响追踪

需求见 prd.md（T1–T9）。依赖子任务 1 已落地的 `consistency` 领域（规范 `.trellis/spec/backend/consistency-check.md`）：台账表 `consistency_ledgers`、`services/consistency/ledger.py`（`load_source`、`build_extract_messages`、`parse_extraction`、`save_ledger`、`ledger_states`）、检查任务的受理与授权重验骨架（`check.py`、`handlers/consistency.py`）。行号以实施时代码为准。

## 1. 边界
- 仍在 `consistency` 领域内扩展，不新建领域。只读正文、分析、剧情记忆、设定演变与台账；只写台账的「上一版」列和新表 `consistency_impacts`。不改正文、摘要、记忆、设定，不改 `consistency_issues`（T9）。
- 不新增「正文已变更」事件：第一级完全在读取时按指纹判定。

## 2. 第一级：派生内容过期（T1、T2，零模型调用）
对给定章节，按当前 `analysis_fingerprints` 值 `F` 判定：
- **章摘要**：该章最新 `ChapterAnalysis` 存在且 `content_hash != F`（与 `document_export/story.py` 同一判定）。无分析时不算过期，另行标注「尚未分析」。
- **剧情记忆**：`origin=auto`、`chapter_id` 为该章、其 `analysis_id` 对应分析的 `content_hash != F` 或章号已变。必须复用 `memory_service._memory_outputs` 的判定逻辑（抽成可共享函数或直接调用），不得另写一套。
- **设定变化**：`SettingProgression` 中 `source="analysis"`、`active`、未撤销，其 `source_change_id` 指向的 `AnalysisChange` 属于该章，且 `source_content_hash != F`。这是对 `source_content_hash` 的首次读取使用；空值的旧记录不判为过期。
- 作品级汇总：一次批量查询得到「有过期派生内容的章节」及各类计数，不逐章查库（章节按 200 一批算指纹）。
- 每项返回跳转所需的标识（章节 id；记忆 id；设定时间线／条目的类型与 id），前端据此链接到章节分析抽屉、记忆页、设定详情的变化页签。链接目标以现有路由为准，实施时核对。
- 作者重新分析该章后，新分析的哈希与 `F` 相同，摘要与记忆的过期提示自然消失；旧分析写入的设定变化若仍保留旧哈希则继续提示，直至其被新分析替换、撤销，或作者在影响页将其标为「已确认无需更新」（见第 4 节 `acknowledged`）。

## 3. 上一版台账（T3、T6）
- `consistency_ledgers` 增加 `prev_facts`（JSON，可空）、`prev_source_hash`、`prev_text_hash`（可空）。
- `save_ledger` 在覆盖已有台账且 `source_hash` 不同时，把旧的 `facts／source_hash／text_hash` 移入 `prev_*`；哈希相同的重复保存不移动。这样无论台账是被「评估影响」还是被一次全书检查刷新，改动前的事实都留有一份。
- 「改动前台账」的取法：台账过期 → 现有 `facts` 即改动前；台账新鲜且有 `prev_*` → `prev_facts` 即改动前；两者都没有 → T6，只显示第一级并提示先运行检查。

## 4. 第二级：评估影响（T3–T5）
任务种类 `consistency.impact`，单章，复用子任务 1 的受理、模型绑定、发送守卫与落库重验骨架（同一处理器模块内新增种类或并列处理器，由实施决定；不得绕过重验）。`resource_key = consistency:{project_id}`，与检查互斥，避免并发改写同一台账。
步骤：
1. 取改动前事实 `old`。台账过期则调用模型重抽（一次 `consistency.extract`）并 `save_ledger`（旧值进入 `prev_*`）；台账已新鲜则不调用模型，直接用 `prev_facts` 作 `old`、当前 `facts` 作 `new`。
2. 事实差异：先做确定性比较——按 `(category, subject_key, 归一化 statement)` 求集合差。集合完全相同 → 结论「未发现剧情事实变化」，不再调用模型。
3. 集合不同时调用一次模型（新模板 `consistency.impact`，输入仅为新旧两组事实），让其判断哪些是真正的剧情事实变化（新增／消失／改变），忽略措辞差异；输出引用输入中的事实 id，服务端校验后丢弃非法引用。模型判定无实质变化 → 同样结论「未发现剧情事实变化」。
   - 与 PRD T3「一次模型调用」的偏差：最多两次（重抽 + 差异判定）。原因是同一段未改动的正文两次抽取的措辞可能不同，纯字符串比较会把改错别字误判为剧情变化，违背 AC2。已在 PRD 更正。
4. 受影响章节（零模型调用）：取变化事实的 `subject_key` 集合，在章号大于该章的章节中查找**新鲜**台账里 `subject_key` 命中的事实，按章聚合为影响项，证据为这些事实的引句与偏移。后续章节中台账缺失或过期的，计入 `uncovered`（数量与章节列表），在结果中明确提示「这些章节尚未抽取或已过期，无法判断」，不静默忽略。
5. 落库：`consistency_impacts` 每章至多一行（`chapter_id` 唯一、级联删除），重新评估时覆盖；同一后续章节在新旧结果中都存在且其证据章节正文指纹未变时，继承原有的确认状态。

`consistency_impacts` 字段：`project_id`、`chapter_id`、`source_hash`／`text_hash`（评估时该章指纹）、`base_source_hash`（改动前台账的指纹）、`changes`（`[{kind: added|removed|changed, category, subject, before, after}]`）、`items`（`[{chapter_id, subjects[], evidence[{fact_id, quote, start, end, text_hash}], status: pending|reviewed|dismissed}]`）、`uncovered`（章节 id 列表）、`acknowledged`（第一级中作者确认无需更新的设定变化 id 列表）、`revision`（确认状态 CAS）、`actor_id`、`task_id`、`model`、时间戳。不存章号。
- 读取时派生：`outdated`（该章当前指纹 != `source_hash`，即评估后又改过，提示重新评估）；每个影响项的 `changed`（其章节当前正文指纹与证据存值不同或章节已删）。
- 需在 `chapter_delete_guard._REVIEWED_FKS` 登记新外键。

## 5. 接口
```
GET   /projects/{pid}/consistency/impacts                      -> 作品级：有过期派生内容或有评估记录的章节列表（含各类计数、台账状态、评估概要）   content.read
GET   /projects/{pid}/consistency/impacts/{chapter_id}         -> 单章：第一级三类明细 + 评估结果（changes、items、uncovered、outdated）+ can_assess 及原因   content.read
POST  /projects/{pid}/consistency/impacts/{chapter_id}/assess  {ai_config_id?} -> 202 TaskOut；无改动前台账 400（提示先运行检查）；与检查互斥 409      ai.use
PATCH /projects/{pid}/consistency/impacts/{chapter_id}         {revision, items?: [{chapter_id, status}], acknowledged?: [...]} -> 单章详情；409 code=stale_revision   content.edit
```
权限、错误包与子任务 1 一致：非协作者 404、查看者写 403。`PATCH` 在尚无评估记录时也可用于保存 `acknowledged`（此时创建一行空评估，`changes` 为空且标记为未评估）。

## 6. 前端
- `ConsistencyPage` 增加「修改影响」页签：章节列表（过期摘要／记忆／设定变化计数、台账状态、最近评估结论）；单章详情抽屉（第一级三类明细与跳转、「评估影响」按钮与进度、事实变化列表、受影响章节卡片与证据跳转、逐项「已复核／无需处理」、未覆盖章节提示、「评估后正文又有修改」提示）。
- 章节编辑页：保存成功后及进入页面时读取单章接口，若有过期派生内容，在页头显示一条不打断写作的提示（如「本章改动后有 N 项派生内容可能过期」），点击进入影响页签并定位该章。只读取、不调用模型；请求失败不影响编辑。
- 证据跳转、只读显隐、请求代次、冲突保留草稿、任务流中断回退轮询，全部沿用子任务 1 的工具函数与做法。

## 7. 备份（v13，迁移 0059）
- `consistency_ledgers` 的三列随现有规则导出；`consistency_impacts` 加入 `RULES`，`items[].chapter_id`、`uncovered`、`evidence` 中的章节 id 重映射（映射不到的影响项丢弃并告警，`uncovered` 中丢弃该 id），`actor_id`、`task_id` 排除，`acknowledged` 中的设定变化 id 按既有设定演变的映射重映射，映射不到则丢弃。
- `VERSION=13`、`APP_SCHEMA="0059"`、`_migrate_v12_to_v13`；同步所有断言版本号的测试与文档常量。

## 8. 取舍
- 受影响章节只基于台账，不做全文字面匹配：结果可解释、与检查口径一致；代价是未抽取的后续章节无法判断，以 `uncovered` 明示并引导运行检查。
- 差异判定「确定性比较 + 必要时一次模型调用」：避免措辞波动造成误报，改错别字的常见情形零额外调用或一次便宜的调用。
- 每章只保留最近一次评估，不留历史。
- 不自动把受影响章节转成 `consistency_issues`：两套清单口径不同（已确认的矛盾 vs 待复核的提示），混在一起会破坏问题状态的含义（AC-P3）。影响页提供「对这些章节运行一致性检查」的快捷入口（范围检查，预填涉及章节的起止），由作者决定。

## 9. 兼容与回滚
- 迁移 0059 给 `consistency_ledgers` 加可空列并新增一张表；降级沿用 0058 的做法：`consistency_impacts` 有数据或任一台账 `prev_facts` 非空时拒绝有损降级。
- 0058 的迁移冻结结构测试若以当前 ORM 推导历史结构，需排除 0059 新增的列（参照 quality-guidelines「迁移冻结结构契约」）。
