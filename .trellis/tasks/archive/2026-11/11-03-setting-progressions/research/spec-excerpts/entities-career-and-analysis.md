# 任务上下文摘录：entities-career-and-analysis

来源：`.trellis/spec/backend/story-entities.md`，原文第206–392行。
源文件SHA256：`f7b532fff1d1cbef2db421cbab8abf1c13b961f5200bd2ce900bd78ef4ebc561`。
这是2026-11-03为避免自动注入截断而保留的连续原文，不修改或降低原规范；涉及摘录外主题时按原索引读取相应章节。若原规范更新，以原文件为准。

---

## 场景：职业等级体系（`10-23-career-system`，迁移 `0008`）

### 1. 范围 / 触发条件

任何写 `careers` / `career_stages` / `character_careers` / `career_history` 的代码：职业 CRUD、`careers/batch`、
角色挂接接口、`characters/with-relations` 的 `careers`、`wizard.careers`、角色向导分配职业、卡片 v2 导入，
以及以后 `analysis-memory` 的阶段回写。**挂接只能经 `career_service.apply_assignment`（新挂用 `assign_new`）写入**。

### 2. 签名

```python
# backend/app/services/career_service.py
MAX_MAIN_CAREERS = 1; MAX_SUB_CAREERS = 3; CAREER_SOURCES = ("manual", "wizard", "ai", "import", "analysis")
async def apply_assignment(db, character, career, *, stage_id=None, note=None, role=None, remove=False,
                           source, chapter_no=None, history_note="") -> CareerHistory | None      # 不 commit
async def assign_new(db, character, career, *, stage_id=None, note="", role=None, source, ...) -> CharacterCareer  # 已挂 → 409
async def sync_stages(db, career, items: list[StageIn]) -> list[CareerStage]   # 整体同步，不 commit
async def append_stage(db, career, name, description="") -> CareerStage          # 追加到末尾（卡片导入）
async def new_career(db, project_id, data: CareerCreate) -> Career               # 同名 409，flush
async def assignments_of(db, character_ids) -> list[tuple[CharacterCareer, Career]]   # 上下文一次查询，主职业在前
async def list_project_assignments(db, user_id, project_id) -> list[AssignmentOut]  # GET /character-careers，角色卡片汇总
async def stages_of(db, career_ids) -> dict[int, list[CareerStage]]               # 按 position
def normalize_career_drafts(obj, *, force_kind=None) -> tuple[list[dict], list[str]]  # career.generate / wizard.careers 共用
async def careers_reference(db, project_id) -> str   # 「剑修（主修）：练气 → 筑基」，无职业时为空串
```

### 3. 契约

- 职业 `(project_id, name)` 唯一（名称 strip 后比较）→ 409 `同名职业已存在`；`kind` ∈ `main`（主修）/ `sub`（副修）。
- 阶段 id 稳定，`position` 由服务层维护为从 1 连续（不建唯一约束，便于整体重排）；同一职业内阶段名唯一（导入按名匹配的前提）。
  POST / PATCH 的 `stages` 整体提交：带 id = 更新、无 id = 新建、缺失 = 删除；提交顺序即新 position。
  缺失的阶段仍被挂接引用 → 409 `该阶段仍有角色处于其中，无法删除`（整个 PATCH 不生效）。
- `character_careers.stage_id` **不设 ondelete**（同 `relationships.type_id`）；删职业时阶段、挂接、历史在同一语句内级联删除
  （`career_history.career_id` 为 CASCADE，`career_name` 冗余仅用于展示）。
- `apply_assignment`：校验职业与角色同项目（400 `职业不存在或不属于该项目`）、阶段属于该职业（400 `阶段不属于该职业`）、
  `role`（给出时）等于职业 `kind`（400 `主修职业只能作为主职业` / `副修职业只能作为副职业`）、上限（409 `该角色已有主职业` /
  `副职业最多 3 个`）。未挂 = 新挂（默认第 1 阶，`changes = {"role": [null, kind], "stage": [null, "第1阶·练气"], "note": [null, ""]}`）；
  已挂 = 对比 `role`（职业类别被改时同步）/ `stage_id` / `note`，有变化才写一条历史（`{"stage": ["第2阶·筑基", "第4阶·金丹"]}`）；
  `remove=True` 删除挂接并写 `{"removed": [职业名, null]}`。阶段以「第N阶·名称」记录（写入时的 position），阶段 / 职业改名后历史仍可读。
- 修改职业 `kind`：对每个持有人调用 `apply_assignment` 同步主 / 副并写历史；会超出上限 → 409，整个 PATCH 不生效。
- 上下文（D4 延伸）：涉及角色卡增加 `职业：剑修·筑基（2/9，瓶颈期）；副职业：丹师·一品（1/9）`；新段落 `【职业体系】`
  （位于【相关组织】之后、【其他角色】之前）对涉及角色所挂的每个职业输出 `■ 剑修（主修）：练气 → 筑基 → …`，
  规则非空时附 `规则：<前 200 字>……`；未被涉及角色挂接的职业不出现。裁剪：关系之后、**相关组织之前**截断职业体系。
- AI：`career.generate`（见下表）只产出草稿，与已有职业同名的 `conflict=true`（同批内重名在规范化时去掉并给 warning，
  没有阶段的职业丢弃）。`character.generate` 在项目有职业时提示词附职业阶梯，`done.result.career_suggestions`
  = `[{career_name, career_id, career_kind, role, stage_name, stage_id, stage_position, note, resolved, reason}]`
  （职业名精确匹配；`role` 取职业类别；阶段名对不上取第 1 阶并写 warning；超出主 1 副 3 → `resolved=false`）。
  `characters/with-relations` 的 `careers: [{career_id, stage_id?, note}]` 经 `assign_new(source="ai")` 写入，失败整体回滚。
- 向导：`wizard.careers`（`resource_key = wizard:{pid}:careers`）删除全部职业（级联清掉挂接与历史）后写入，
  `done.result = {kind: "careers", items: CareerOut[], warnings}`；解析不到任何职业 → failed 且保留旧数据。
  角色向导在项目已有职业时要求每个角色输出 `careers: [{name, stage, role, note}]`，按名匹配后 `assign_new(source="wizard")`；
  职业对不上 / 超限 / 重复 → 忽略并写入 `done.result.warnings`，阶段对不上 → 第 1 阶 + warning；`done.result` 另含
  `assignments: AssignmentOut[]`。项目没有职业时提示词与写入完全不变（AC6）。
- 项目级挂接列表 `GET /projects/{pid}/character-careers` → `AssignmentOut[]`（按角色、主职业在前；先校验项目归属，他人 404），供角色卡片汇总主 / 副职业一次取回，前端不得按职业逐个请求 `holders` 拼装。
- 卡片 v2：见下方「卡片导入导出」的 v2 补充。

| kind | resource_key | 路由 | `done.result` |
|---|---|---|---|
| `career.generate` | 无 | `POST /projects/{pid}/careers/generate {ai_config_id?, count 1–5, kind?, instruction}` | `{drafts: [{name, kind, description, requirements, abilities, rules, stages: [{name, description}], conflict}], warnings}` |
| `wizard.careers` | `wizard:{pid}:careers` | `POST /projects/{pid}/wizard/careers {career_count 1–12, ...}` | `{kind: "careers", items, warnings}` |

### 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 同名职业（新建 / 改名 / 批量中任一条） | 409 `同名职业已存在`（批量全部回滚） |
| `stages` 为空 | 400 `职业至少需要 1 个阶段` |
| 阶段名重复 / 为空 | 400 `同一职业内阶段名称不能重复` / `阶段名称不能为空` |
| `stages[].id` 不属于该职业（含他项目） | 400 `阶段不属于该职业` |
| 删除仍被引用的阶段 | 409 `该阶段仍有角色处于其中，无法删除` |
| 第 2 个主职业 / 第 4 个副职业 / 重复挂 | 409 `该角色已有主职业` / `副职业最多 3 个` / `该角色已挂该职业` |
| 主副类别不符 | 400 `主修职业只能作为主职业` / `副修职业只能作为副职业` |
| 请求体中的职业 id 不属于本项目 | 400 `职业不存在或不属于该项目` |
| 路径中的职业 / 角色 / 挂接不属于本项目或他人 | 404 `职业不存在` / `角色不存在` / `职业挂接不存在` |

### 5. Good / Base / Bad

- Good：`apply_assignment(db, ch, sword, stage_id=jindan.id, source="analysis", chapter_no=12)` → 历史 `{"stage": ["第2阶·筑基", "第3阶·金丹"]}`。
- Base：`apply_assignment(db, ch, sword, note="瓶颈期", source="manual")` → 只改备注也写历史；值相同返回 `None`。
- Bad：`assignment.stage_id = jindan.id; await db.commit()` —— 没有历史、绕过上限与归属校验。

### 6. 必需的测试

`tests/test_careers.py`（AC1 阶段同步 / 重排 / 409、AC2 上限与类别、AC3 历史、类别变更同步、批量原子性、跨项目 400、级联清理）、
`tests/test_career_tasks.py`（career.generate 草稿与 conflict、character.generate 职业建议 + with-relations、wizard.careers 覆盖、
角色向导分配与 warnings、无职业时提示词不变、AC5 上下文、裁剪顺序）、`tests/test_cards.py::test_cards_v2_*`、
`tests/test_migrations.py::test_0008_careers`、`tests/test_careers.py::test_project_assignments`、`test_cross_user_isolation`（职业相关路由均 404，含 `character-careers`）。


---

## 场景：章节分析与回写（`10-23-analysis-memory`，迁移 `0009`）

### 1. 范围 / 触发条件

`analysis_service`（分析任务、状态、历史、排队）、`writeback_service`（回写条目的生成 / 应用 / 撤销 / 确认 / 拒绝）、
`task_engine/handlers/analysis.py`、`chapter.revise`（`generation_service` 的 revise 模式）、`api/v1/analysis.py`。
以后的 `foreshadows`（读 `foreshadow_candidates`）、`knowledge-base`（读 summary / plot_points）都基于这里的数据。

### 2. 签名

```python
# analysis_service
def content_hash(text) -> str                       # sha256(utf-8)
def compute_metrics(content) -> {words, paragraphs, avg_paragraph_chars, dialogue_ratio}   # 纯函数
def normalize_scores(value) -> {overall, pacing, engagement, coherence}  # 1–10 一位小数；overall = 有值项平均（服务端复算）
def normalize_analysis(obj) -> (data, warnings)     # 纯函数；非 dict → JSONExtractError
async def request_analyze(db, engine, user_id, chapter_id, req) -> (TaskOut, created: bool)
async def analyze_pending(db, engine, user_id, project_id, req) -> AnalyzePendingResult(queued, task_ids)
async def enqueue_auto(ctx, project_id, chapter_id) -> int | None
async def project_states / chapter_state / list_analyses / get_analysis
# writeback_service
async def apply_analysis(db, analysis, data) -> list[AnalysisChange]   # 不 commit，与分析同一事务
async def set_character_field(db, character, "current_state" | "life_status", value) -> before   # 角色状态的回写入口
async def revert_change / confirm_change / reject_change(db, user_id, change_id) -> AnalysisChangeOut   # commit
# memory_service（knowledge-base，见 retrieval.md「剧情记忆」）
async def sync_from_analysis(db, chapter, analysis) -> list[StoryMemory]   # 不 commit：apply_analysis 之后、同一事务
```

### 3. 契约

- **数据**：每次分析一行 `chapter_analyses`（历史保留，最新 = `max(id)`）：`content_hash`（分析时正文）、`chapter_no`（冗余，回写比较先后）、
  summary（≤1000）、plot_points `[{text, importance 1–5}]`、hooks `[{type 悬念|冲突|情感|反转|其他, strength 1–10, text}]`、
  conflict `{level, types}`、emotion `{tone, start, middle, end}`、changes_raw（规范化后的四类变化）、foreshadow_candidates（原样）、
  scores、metrics、suggestions `[{id 从 1 起, category 节奏|对话|描写|冲突|人物|其他, priority high|medium|low, text}]`、warnings、model。
- **状态**（`GET /projects/{pid}/analysis/statuses`、`GET /chapters/{cid}/analysis-status`）：有排队 / 运行中的 `chapter.analyze` → `analyzing`；
  无分析 → `none`；最新分析 hash == 当前正文 hash → `analyzed`；否则 `stale`。手动编辑只会让状态变 `stale`，不触发分析。
- **触发**（D2）：`ChapterHandler.on_success`（generate / polish / revise 成功）→ `enqueue_auto`：**部署开关 AND 项目开关**才自动分析——部署开关 `AUTO_ANALYZE_ENABLED`
  （`.env.example` / `backend/.env.example` / `docker-compose.yml` 均已透传，默认 true；关闭后忽略项目开关，手动分析与一键分析不受影响）
  （`Settings.auto_analyze_enabled`，默认 true；**测试的 `make_settings` 默认 false**，自动分析相关测试显式开启）与项目 `auto_analyze`
  都为真、章节存在且正文非空、同章没有**排队中**的分析（运行中的不算）才入队；任务 `user_id` = 项目所有者，不做请求级配置校验，
  `input = {request: {chapter_id}, chapter_id, auto: true}`。手动 `POST /chapters/{cid}/analyze`：同章已有排队中的分析 → 200 返回那条任务，
  否则 202 新任务。一键 `POST /projects/{pid}/analysis/analyze-pending`：按 `chapter_no, id` 为 none / stale 且正文非空、无排队中或运行中分析的章节逐个入队（与 enqueue_auto 不同：一键分析不为运行中的章节再排一次）。
- **排队**：`serial_key = analysis:{project_id}`、`resource_key = NULL`，同项目串行（按入队顺序回写），不同项目并行；分析失败只让本任务 failed。
  分析**不写生成记录**；`on_abort` 无需还原（只在成功时一次性落库）。
- **提示词**（`ai/prompts/analysis.py`）：作品信息 + 本章涉及角色的当前状态（主角 ∪ 手动出场 ∪ 本章正文提及；存活、当前状态、组织职位、职业阶段）
  + 他们之间的关系（亲密度、状态、描述）+ 项目组织名 + 相关职业阶梯 +（有可引用伏笔时）【已有伏笔】清单 + 正文 + 评分标准（1–3 / 4–5 / 6–7 / 8–9 / 10）+ JSON 结构；
  要求只报告正文里发生的变化、给出**变化后的绝对值**、建议数量与分数挂钩。
- **回写**（D3 / R4）：
  | kind | field_key | before / after | 处理 | 写入入口 |
  |---|---|---|---|---|
  | `character_state` | `char:{cid}:current_state` | 文本 | 自动 | `set_character_field` |
  | `life_status` | `char:{cid}:life_status` | `alive|dead|missing|other` | **待确认** | 确认后 `set_character_field` |
  | `relationship_update`（每字段一条，`target.field`） | `rel:{rid}:{intimacy|status|description}` | 字段值 | 自动 | `apply_change(source="analysis", chapter_no, note="第 N 章分析")` |
  | `relationship_create` | `relpair:{low}:{high}` | `null` / `{role_a, role_b, category, intimacy, status, description}` | **待确认** | 确认后 `create_relation`（after 补 `a_id/b_id/type_id`，target 补 `relationship_id`） |
  | `career_stage`（仅已挂接职业） | `career:{cid}:{career_id}` | `{stage_id, stage: "第N阶·名称"}` | 自动 | `apply_assignment(stage_id, source="analysis")` |
  | `org_membership` | `org:{org_id}:{cid}` | `null` 或 `{position, status}` | 自动 | `apply_membership` |
  | `foreshadow_resolve` / `foreshadow_plant_planned` / `foreshadow_new` | 见文末「伏笔管理」 | | | `apply_foreshadow` |
  - 名字按「名字 → 别名」精确匹配（多个同名 / 同别名视为对不上）；组织 / 职业 / 阶段按名称精确匹配；对不上 → `skipped`，`field_key` 为空，
    reason 如 `未找到名为「X」的角色`、`「A」与「B」之间没有已有关系`、`「X」未挂接职业「Y」`、`职业「Y」没有名为「Z」的阶段`。
  - after == 当前值 → 不建条目（重复分析幂等）；同 `field_key` 已有**更晚章节**的 `applied` → `skipped`「已有第 N 章的更新」；
    `life_status` / `relationship_create` → `pending`；其余在 `begin_nested()`（SAVEPOINT）中写入，`AppError` → `failed`（reason = detail），
    意外异常 → `failed`「写入失败：服务器内部错误」，都不影响其他条目。已有关系的变化**只改亲密度 / 状态 / 描述**，不改类型。
  - 组织动作：`join` → `{position（给出或原值或空）, status（给出或 active）}`；`leave` → 原职位 + `left|expelled|dead`（未是成员时不建条目）；
    `update` 未是成员时按 join 处理。
  - 撤销（仅 `applied`，否则 409 `只能撤销已写入的条目`）：当前值 == after 才经同一入口写回 before（关系 / 境界历史 note `撤销第 N 章分析`；
    新建的关系整条删除），否则 409 `该项已被后续修改，无法撤销`；状态 → `reverted`。
  - 确认（仅 `pending`，否则 409 `只能确认待确认的条目`）：重新校验——同 `field_key` 有更晚章节 applied → 409 `已有第 N 章的更新，无法确认`；
    角色已删除 → 409 `对应的设定已不存在`；存活状态已不等于 before → 409 `该项对应的设定已被修改，无法确认`；关系已存在 → 409 `这两个角色之间已有关系`。
    拒绝（仅 `pending`，否则 409 `只能拒绝待确认的条目`）→ `rejected`。均写 `resolved_at`（确认另写 `applied_at`）。
- **按建议重写**（`chapter.revise`，`POST /chapters/{cid}/revise {analysis_id, suggestion_ids[], instruction, preserve[], target_words?, ai_config_id?}`）：
  `resource_key = chapter:{id}`（与生成 / 润色互斥，创建即抢占 `generating`）；不自动续写；提示词 = 与整章生成相同的上下文 + 原文全文 +
  勾选建议（按勾选顺序编号）+ 补充要求 + 必须保留 + 目标字数（未填 = 原文字数，下限 300）；成功同 polish：`ensure_baseline` → 覆盖正文 →
  生成记录 `kind=revise` → 版本 `source=revise`；成功后同样 `enqueue_auto`。`TaskOut.analysis_id`：`task_service.to_out` 取自 `input.request.analysis_id`（仅 `chapter.revise` 有值，其余为 `null`），
  前端据此用重写引用的分析与新分析对比评分（任务面板重试 / 其他标签页发起的重写同样适用）。
- **归属**：所有入口先校验项目归属（分析 / 条目按 `project_id → projects.user_id`），他人 404（`分析结果不存在` / `回写条目不存在` / `章节不存在` / `项目不存在`）。

### 4. 必需的测试

`tests/test_analysis.py`（指标 / 规范化纯函数、AC1 结构与历史、空正文 / 解析失败、AC2 过期与项目 / 部署开关、AC3 一键分析串行顺序与去重、
跨项目并行与失败不阻塞生成、`enqueue_auto` 去重（排队中不重复、运行中可再排、空正文 / 项目开关关闭不入队）、AC6 前情摘要、AC8 越权）、
`tests/test_writeback.py`（AC4 分流与历史来源、确认 / 拒绝 / 重新校验（含确认时「更晚章节优先」409）、AC5 撤销与冲突（**每种 kind** 被手动修改后撤销 409）、
更晚章节优先、对不上只提示、SAVEPOINT 单条失败与**部分写入后失败的回滚**）、`tests/test_revise.py`（AC7 版本 / 生成记录 / 自动重新分析、
`TaskOut.analysis_id`、校验、互斥与失败复位）、`tests/test_tasks.py`（serial_key 同键串行 / 异键并行 / 与每用户上限共同生效 / 重启后释放并继续、on_success）。
这些测试须对被测逻辑敏感：删掉「更晚章节优先」、撤销比对、确认重新校验或 `begin_nested()` 中任一处，至少一个用例失败、`tests/test_context_builder.py::test_summary_prefers_analysis_summary`、
`tests/test_migrations.py::test_0009_chapter_analysis`、`test_cross_user_isolation`（新增分析路由）。

### 5. 错误写法

```python
character.current_state = "重伤"; await db.commit()          # 没有回写条目，无法撤销，也不受「更晚章节优先」约束
rel.type_id = enemy.id                                     # 自动回写改了关系类型（MuMu 的反例）
```


---

