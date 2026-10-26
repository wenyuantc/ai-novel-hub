# 设计：章节分析与回写

## 1. 边界

- **新增后端模块**
  - `analysis_service`：分析的 prepare/run 与落库、客观指标、过期判断、历史查询
  - `writeback_service`：把分析结果转换成变更条目，负责应用、撤销、确认、拒绝
  - `api/v1/analysis.py`
  - 两个任务类型：`chapter.analyze` 和 `chapter.revise`（按建议重写）
- **任务引擎扩展**（通用能力，后续多步任务也会用到）
  - `serial_key`：同键任务排队后串行执行
  - `TaskHandler.on_success` 回调（可选）
- **修改的现有模块**
  - `context_builder`：前情提要改用章节摘要
  - `generation_service`：新增 revise 模式
  - `VERSION_SOURCES`：加入 `revise`
  - `Project`：新增 `auto_analyze` 字段
  - 组织成员、角色状态的写入改走 `writeback_service`，保证每次修改都留下前后值

## 2. 数据模型（迁移 `0009_chapter_analysis`）

> 开工时确认编号。当前最新是 `0008_careers`；封面任务如果抢先占用，本任务的编号顺延。

**`projects.auto_analyze`**：bool，默认 true，迁移时回填 true。

**`tasks.serial_key`**：String(100)，可空，加索引（见 §3）。

**`chapter_analyses`**（每次分析一行，历史保留）

| 字段 | 说明 |
|---|---|
| id, project_id CASCADE, chapter_id CASCADE（index）, task_id SET NULL | |
| content_hash String(64) | 分析时正文的 sha256。章节当前正文的 hash 与它不同，即视为已过期 |
| chapter_no int | 冗余字段，用于回写时比较章节先后 |
| summary Text | |
| plot_points / hooks / conflict / emotion JSON | 结构见 §5 |
| changes_raw JSON | 模型输出的原始变化，便于排查 |
| foreshadow_candidates JSON | 原样保存，留给 `foreshadows` 使用（D1） |
| scores JSON | `{overall, pacing, engagement, coherence}`，取值 1.0–10.0，overall 由服务端对三项求平均后复算 |
| metrics JSON | 本地规则计算：`{words, paragraphs, avg_paragraph_chars, dialogue_ratio}` |
| suggestions JSON | `[{id, category, priority(high/medium/low), text}]`，id 为本次分析内的序号 |
| model String, created_at | |

章节的"最新分析"取 `max(id) WHERE chapter_id=?`。分析状态的判定：
- 有排队或运行中的 `chapter.analyze` 任务：**分析中**
- 没有分析记录：**未分析**
- 最新分析的 hash 等于当前正文 hash：**已分析**，否则**已过期**

**`analysis_changes`**（回写条目）

| 字段 | 说明 |
|---|---|
| id, analysis_id CASCADE, project_id CASCADE, chapter_no | |
| kind String(24) | `character_state`、`life_status`、`relationship_update`、`relationship_create`、`career_stage`、`org_membership` |
| target JSON | 例如 `{character_id, name}`、`{relationship_id, a_id, b_id}`、`{character_id, career_id}`、`{organization_id, character_id}` |
| field_key String(120) | 规范化的字段标识，如 `char:12:current_state`、`rel:5:intimacy`、`career:12:3`、`org:4:12`，用于 R4 的"更晚章节优先"判断和撤销校验 |
| before JSON, after JSON | |
| status String(12) | `applied`、`pending`、`reverted`、`rejected`、`skipped`、`failed` |
| reason String(200) | 跳过或失败的原因，或未匹配到对象的提示 |
| applied_at / resolved_at | |

`relationship_update` 会拆成多条，每个字段（intimacy、status、description）各一条，这样可以逐条撤销。

## 3. 任务引擎扩展

- **`serial_key`**：调度器扫描排队任务时，如果有同 `serial_key` 的任务正在运行，就跳过这一条，留到下一轮。与 `resource_key` 的区别：`resource_key` 冲突时直接拒绝，`serial_key` 冲突时排队等待。分析任务使用 `serial_key = analysis:{project_id}`（R3），一个项目同一时间只跑一个分析；同一项目内按 id 先进先出，不同项目之间并行。
- **`on_success(ctx, task)`**：`_finalize` 在状态为 succeeded 后调用（可选方法，用 `getattr` 探测），吞掉异常只记日志，失败不影响原任务状态。`ChapterHandler`（generate、polish）和 `ReviseHandler` 用它来调用 `analysis_service.enqueue_auto(project_id, chapter_id)`。
- **`enqueue_auto`**：
  1. 项目的 `auto_analyze` 为 false，不入队；
  2. 已有该章的 `chapter.analyze` 处于排队状态，不入队；
  3. 否则以项目所有者作为任务的 user_id 创建任务（计入该用户的并发上限，可在其任务面板看到与取消），不做 AI 配置的请求级校验，配置在运行时解析（未配置可用模型时任务失败并给出提示）。
  - 运行中的分析不算"已排队"。这样生成完成时，如果上一次分析还在跑，会再排一次，正好分析最新的正文。
- **一键分析**：`POST /projects/{pid}/analysis/analyze-pending`。按 `chapter_no` 顺序，为未分析或已过期且正文非空的章节逐个入队（同样会跳过已排队的），返回入队数量。

## 4. 回写规则（`writeback_service`，D3 / R4）

模型的输入包含本章涉及角色的当前状态：当前状态文本、存活状态、彼此之间的关系（亲密度、状态）、境界、所属组织和职位（复用 `cast_service` 的选角结果和 `context_builder` 的数据）。模型输出的是**变化后的绝对值**，不是增量。这样重复分析是幂等的，不会把同一个变化叠加两次。

| 变化类型 | 处理方式 | 写入入口 |
|---|---|---|
| `character_state`（当前状态文本） | 自动 | `writeback_service` 直接改字段并记录前后值 |
| `life_status`（死亡、失踪等） | **待确认** | 确认后同上 |
| `relationship_update`（已有关系的亲密度、状态、描述） | 自动 | `relationship_service.apply_change(source="analysis", chapter_no)` |
| `relationship_create`（新关系） | **待确认** | 确认后调用 `create_relation(source="analysis")` |
| `career_stage`（升阶或降阶，只在已挂接的职业内） | 自动 | `career_service.apply_assignment(source="analysis")` |
| `org_membership`（加入、离开、职位、成员状态） | 自动 | 新增 `organization_service.apply_membership(...)`，记录前后值 |

- **名字匹配**：按名字或别名精确匹配本项目角色、组织、职业、阶段。对不上的条目记为 `skipped`，原因写"未找到名为「X」的角色"，只做提示。
- **更晚章节优先**：写入前，查询同一 `field_key` 最近一条 `status=applied` 的条目。如果它的 `chapter_no` 大于本次章节号，本条记为 `skipped`，原因为"已有第 N 章的更新"。
- **无变化**：after 等于当前值时，不生成条目。
- **撤销**：当前值等于 after 时，经同一入口写回 before（来源仍为 analysis，`note="撤销第 N 章分析"`），状态改为 `reverted`；否则返回 409「该项已被后续修改，无法撤销」。
- **确认和拒绝**：只能作用于 `pending` 条目。确认时，按当前状态重新校验，例如关系在这期间已被手动建立，返回 409，再执行写入，状态改为 `applied`。拒绝时状态改为 `rejected`。
- 回写和分析结果在同一个事务里落库。单条写入失败（例如校验错误）时，该条记为 `failed`，不影响其他条目；实现上每条写入用 SAVEPOINT（`begin_nested`）包住。

## 5. 分析提示词与输出（`ai/prompts/analysis.py`）

输出 JSON，结构如下：

```json
{
  "summary": "...",
  "plot_points": [{"text", "importance": 1-5}],
  "hooks": [{"type": "悬念|冲突|情感|反转|其他", "strength": 1-10, "text"}],
  "conflict": {"level": 1-10, "types": []},
  "emotion": {"tone", "start", "middle", "end"},
  "character_changes": [{"name", "current_state?", "life_status?": "alive|dead|missing|other"}],
  "relationship_changes": [{"a", "b", "intimacy?", "status?", "description?", "new_relation?": {"role_a", "role_b", "category"}}],
  "career_changes": [{"name", "career", "stage"}],
  "organization_changes": [{"name", "organization", "action": "join|leave|update", "position?", "status?"}],
  "foreshadows": [{"action": "plant|resolve", "text", "keyword", "expected_resolve_chapter?"}],
  "scores": {"pacing", "engagement", "coherence"},
  "suggestions": [{"category": "节奏|对话|描写|冲突|人物|其他", "priority", "text"}]
}
```

- 评分标准写进提示词，分档说明 1–3、4–5、6–7、8–9、10。建议数量与分数挂钩：低于 5 分给 3–5 条，8 分以上给 0–2 条。
- 只把"本章涉及角色及其当前状态"交给模型，明确要求只报告本章正文里确实发生的变化。
- 解析复用现有的 `extract_json`，对枚举和取值范围做规范化，越界的值截断，非法项丢弃并写入 `warnings`，最终放进 `done.result`。
- `metrics` 由本地计算：对白占比为引号（「」『』“”"）内字数除以总字数，段落按空行切分。

## 6. 按建议重写（`chapter.revise`，R6）

- **请求**：`POST /chapters/{cid}/revise {analysis_id, suggestion_ids[], instruction, preserve[], target_words?, ai_config_id?}`
  - `preserve` 的预置选项：保持整体结构、保持人物性格、保留关键对白，也可以自定义。
  - `analysis_id` 必须属于本章。`suggestion_ids` 必须是该次分析里的 id。至少选一条建议，或者填写 instruction。
- **执行**：复用 `generation_service`，新增 mode `revise`，资源键为 `chapter:{id}`，与生成、润色互斥。
  - 提示词组成：原文全文、勾选的建议、补充要求、需要保留的要素、上下文（同整章生成）、目标字数（未填时取原文字数）。新建 `build_revise_messages`。
  - 成功后的写入与 polish 一致：`ensure_baseline`、覆盖正文、记一条版本（`source=revise`）、写一条生成记录（`kind=revise`）。
  - 不做自动续写。
- **衔接**：`on_success` 触发自动分析（D2）。前端在新的分析完成后，用这次重写引用的 `analysis_id` 和新分析做评分对比。

## 7. 上下文摘要（R5）

- `context_builder`：组装"更早章节"时，批量查出这些章节最新一次分析的 summary（一条查询，用子查询取 max(id)）。有摘要的行显示为 `第N章 标题：<summary>`，没有的仍按原逻辑截取正文前 200 字。
- 文件头注释补充摘要的来源。其余裁剪顺序不变。

## 8. API（前缀 `/api/v1`，都先做归属校验，失败返回 404）

| 端点 | 说明 |
|---|---|
| `POST /chapters/{cid}/analyze {ai_config_id?}` | 返回 202 `TaskOut`。同一章已有排队中的分析时，直接返回那条任务（200），不报错 |
| `POST /projects/{pid}/analysis/analyze-pending` | 返回 `{queued, task_ids}` |
| `GET /projects/{pid}/analysis/statuses` | `[{chapter_id, status, latest_analysis_id, scores?}]`，供章节列表使用 |
| `GET /chapters/{cid}/analyses` | 历史列表（不含大字段）：`{id, created_at, scores, metrics, stale, task_id}` |
| `GET /analyses/{aid}` | 详情，包含 changes 条目 |
| `POST /analysis-changes/{id}/revert` / `confirm` / `reject` | 返回更新后的条目 |
| `POST /chapters/{cid}/revise` | 返回 202 `TaskOut` |
| `PATCH /projects/{pid}`，增加 `auto_analyze` | 复用现有项目更新接口 |

分析不写生成记录，和其他实体任务一致；`revise` 写生成记录。

## 9. 前端

- **章节编辑器**：右侧 AI 助手的 Tabs 新增"分析"。也可以做成工具栏按钮打开 Drawer，以组件规范为准；倾向做成 Drawer，避免右侧面板过于拥挤。分析面板的内容：
  - 顶部：状态标签（未分析、分析中、已分析、已过期），以及"分析本章"或"重新分析"按钮
  - 评分卡：四项评分，与上一次分析的差值用 ↑↓ 标出；客观指标
  - 摘要、情节点、钩子列表
  - 建议：可勾选，下方有"按建议重写"表单（补充要求、保留要素、目标字数），提交后订阅任务输出
  - 回写清单：按类型分组，每条显示"前 → 后"和状态标签。已写入的条目可撤销，待确认的可确认或拒绝，跳过的显示原因。待确认的数量在 Tab 标题上显示为徽标
  - 历史：评分走势，用简单的折线或表格，不引入图表库，可以用 antd 的 `Progress` 或纯 SVG；可以切换查看任意一次分析
- **章节列表**：新增"分析"列，显示状态标签和整体评分。
- **项目概览**：新增"自动分析"开关和"分析全部未分析章节"按钮。
- **任务面板**：为 `chapter.analyze` 和 `chapter.revise` 补充标签和跳转（跳到章节编辑器的分析面板）。

## 10. 兼容、风险与回滚

- **迁移**：新增两张表、两列（`projects.auto_analyze`、`tasks.serial_key`），降级时删除。迁移测试要指定版本号。
- **调度器**：改动 `serial_key` 逻辑后，原有的调度测试必须全部通过。新增测试覆盖"同键串行、异键并行、与每用户上限共同生效"。
- **自动分析会额外消耗 token**：项目开关默认开启（D2）。概览页的开关旁说明"每章生成后会多一次 AI 调用"。
- **回写的正确性**：重点测试"更晚章节优先"和撤销时的冲突检测。
- **回滚**：回退代码后执行 `alembic downgrade -1`。已写入的回写都在各实体的历史中有记录。
