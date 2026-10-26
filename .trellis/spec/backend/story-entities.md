# 结构化设定：角色 / 组织 / 关系 / 卡片 / 章节上下文

> 来源任务：`10-23-story-entities`；职业等级体系来自 `10-23-career-system`；章节分析与回写（文末）来自 `10-23-analysis-memory`。表结构与外键规则见 [database-guidelines.md](./database-guidelines.md)，
> 任务契约见 [error-handling.md](./error-handling.md)「后台任务与 SSE 契约」。

---

## 场景：人物关系写入（`relationship_service.apply_change`）

### 1. 范围 / 触发条件

任何写 `relationships` 的代码：手动 CRUD、`characters/with-relations`、`relationships/batch`、卡片导入、角色向导，
以及以后 `analysis-memory` 的自动回写。**不允许**绕过 `apply_change` 直接改关系字段。

### 2. 签名

```python
# backend/app/services/relationship_service.py
TRACKED_FIELDS = ("a_id", "b_id", "type_id", "intimacy", "status", "description")
async def apply_change(db, rel, values, *, source, chapter_no=None, note="") -> RelationshipHistory | None  # 不 commit
async def create_relation(db, project_id, *, a_id, b_id, type_id=None, role_a=None, role_b=None, category="other",
                          intimacy=0, status="normal", description="", source, chapter_no=None, note="") -> Relationship
async def resolve_type(db, project_id, *, type_id=None, role_a=None, role_b=None, category="other")
    -> tuple[RelationshipType, bool]   # flipped=True 表示匹配到反向类型，调用方需互换 a/b；匹配不上新建自定义类型（flush）
async def find_type(db, project_id, role_a, role_b) -> tuple[RelationshipType | None, bool]   # 预置优先
async def find_pair(db, a_id, b_id) -> Relationship | None
async def relations_among(db, character_ids) -> list[tuple[Relationship, RelationshipType]]
```

### 3. 契约

- 语义：「a 是 b 的 `role_a`，b 是 a 的 `role_b`」。a 视角对方显示 `role_b`，b 视角对方显示 `role_a`
  （`RelationshipOut.other_role / self_role / other_name / other_id`，仅 `?character_id=` 查询时有值）。
- 一对角色一条：`pair_low/pair_high = min/max(a_id, b_id)`，唯一约束兜底；`apply_change` 先查重 → 409 `这两个角色之间已有关系`。
- `apply_change(rel)`：`rel.id is None` 视为新建（须先设 `project_id`），未给的字段用默认值（0 / normal / ""）。
  校验两端是本项目的不同角色（400 `角色不存在或不属于该项目` / `不能与自己建立关系`）、类型对本项目可见（预置或本项目自定义）、
  亲密度 -100～100、状态 ∈ `RELATION_STATUSES`、`source` ∈ `RELATION_SOURCES`（否则 `ValueError`，属编程错误）。
- 有变化才写历史，`changes = {字段: [旧, 新]}`；类型以「role_a/role_b」（对称为 role_a）记录在 `type` 键、方向以「A名→B名」记录在
  `direction` 键（名字取写入时的值，类型被删后仍可读）；新建时全部旧值为 `null`。无变化返回 `None`、不写历史。
- 自动回写（analysis 等）**只追加历史**，不得把变化描述写进类型或覆盖 `description` 之外的语义字段（MuMu 的反例：覆盖关系名）。
- 预置类型（`project_id IS NULL`，20 种，`PRESET_RELATIONSHIP_TYPES`）只读：删除 → 400 `预置关系类型不能修改或删除`；
  自定义类型按 (role_a, role_b) 及其反向去重 → 409 `该关系类型已存在`；仍被引用 → 409 `该关系类型仍被关系使用，无法删除`。
- `type_id` 外键**不设 ondelete**（NO ACTION）：删除项目时类型与关系在同一语句内级联删除，RESTRICT 会因触发顺序失败。
- 预置类型的来源：生产由迁移 `0007` 插入（迁移内冻结一份列表），测试的 `create_all` 由
  `RelationshipType.__table__` 的 `after_create` 事件插入。两处列表由 `test_0007_story_entities` 断言一致。

### 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 同一对角色（含反向）已有关系 | 409 `这两个角色之间已有关系` |
| a == b | 400 `不能与自己建立关系` |
| 角色不属于本项目 | 400 `角色不存在或不属于该项目` |
| type_id 不可见（他项目自定义 / 不存在） | 400 `关系类型不存在或不属于该项目` |
| PATCH `a_id` 不是这对角色之一 | 400 `a_id 必须是这段关系的一方` |
| 批量中任一条失败 | 全部回滚 |

### 5. Good / Base / Bad

- Good：`create_relation(..., role_a="徒弟", role_b="师父")` → 匹配预置「师父/徒弟」反向 → 存为 b 是 a 的师父。
- Base：`apply_change(rel, {"intimacy": 80}, source="manual", chapter_no=3)` → 历史 `{"intimacy": [50, 80]}`。
- Bad：`rel.intimacy = 80; await db.commit()` —— 没有历史，`analysis-memory` 无法追溯。

### 6. 必需的测试

`tests/test_relationships.py`（视角称呼、唯一、历史、调换方向、级联清理、类型只读 / 去重 / 引用中、跨项目类型不可用、
按称呼反向匹配、批量原子性、with-relations、图谱）；`tests/test_migrations.py::test_0007_story_entities`。

---

## 场景：组织

- `organizations (project_id, name)` 唯一 → 409 `同名组织已存在`。`parent_id` SET NULL（删上级后下级变顶级）。
- `check_parent`：上级须同项目（400 `上级组织不存在或不属于该项目`）；沿上级链最多 50 层，遇到自身 → 400
  `不能把组织自身或其下级组织设为上级`。PATCH 中 `parent_id` / `power_level` 显式 `null` = 清空（`model_fields_set`）。
- 成员 `(organization_id, character_id)` 唯一 → 409 `该角色已是该组织成员`；角色须同项目 → 400。
- `with-members`：组织 + 成员单事务（AI 生成组织预览后保存）。测试：`tests/test_organizations.py`。
- 章节分析回写成员关系只经 `organization_service.apply_membership(db, org, character, values | None) -> before`：
  `values = {position?, status?}`（未给的沿用原值，新成员默认 `active`），`None` = 移除；校验同项目 / 职位 ≤50 / 状态合法；
  返回写入前快照 `{position, status}` 或 `None`，不 commit。手动成员接口不变。

---

## 场景：卡片导入导出（`ainovel.cards` v1）

### 契约

- 导出 `POST /cards/export {character_ids, organization_ids}`：只含勾选项；关系两端都在集合内才导出；成员角色在集合内才导出；
  上级也在集合内才写 `parent_name`。**任何层级都没有 `id` / `*_id` 键**（`test_cards::_id_keys`）。
- 预览 / 导入的 body 为 `{data: <原样 JSON>}`（不引入 multipart）。`data` 声明为 `Any`，结构在 `card_service.parse_card`
  中校验，错误一律 400 中文：大小（紧凑序列化 `separators=(",", ":")`、`ensure_ascii=False` 后 UTF-8 > 2MB，与前端 `JSON.stringify` 口径一致，导出文件的缩进不计入）`卡片文件过大，上限为 2MB`、非对象、`format` / `version` 不符、字段错误
  `卡片文件格式不正确：<loc>: <msg>`。不信任卡片里的任何 ID（多余字段忽略）。
- 卡片内重名只取第一个并给 warning。导入时同名冲突按 `resolutions.{characters|organizations}[name]`：`skip`（默认）映射到目标已有对象；
  `rename` 用 `rename_for` 追加「（导入）」「（导入2）」……（与目标已有名 + 卡片内名 + 本次已改名都不冲突，超长截断原名）。
- 关系两端 / 成员角色先查「卡片名 → 新 id」映射，再按名称查目标项目已有对象；对不上、指向自己、称呼只有空白、同一对已有关系 → 跳过并记 warning（预览中标 `unresolved`），不中断整个导入。
  关系类型走 `resolve_type`（反向匹配互换两端，匹配不上建自定义类型），写入走 `apply_change(source="import")`。
- 新建组织的上级用内存中的上级链检测循环，成环 → warning 并置空。整个导入一个事务，最后 commit。
- 测试：`tests/test_cards.py`（跨项目往返 + 重命名、skip 关联已有、无法关联与循环、格式 / 大小 400、`rename_for`）。
- **v2（`10-23-career-system`）**：导出 `version: 2`；角色项增加 `careers: [{career_name, role, stage_name, note}]`，顶层增加
  `careers: [{name, kind, description, requirements, abilities, rules, stages: [{name, description}]}]`（只含所导出角色挂接的职业）。
  `parse_card` 接受 `SUPPORTED_CARD_VERSIONS = (1, 2)`，其他版本 400 `不支持的卡片版本：N（当前支持 1、2）`；v1 没有职业，按原逻辑导入。
  导入：职业按名匹配目标项目，匹配上复用（卡片里多出的阶段追加到末尾）、匹配不上新建；挂接的阶段按名匹配，对不上追加到末尾，
  阶段名为空取第 1 阶；经 `assign_new(source="import")` 写入，职业对不上 / 类别不符 / 超限 / 已挂 → warning 跳过。
  预览增加 `careers: [{name, status: new|reuse, stages_added: [阶段名]}]`；结果增加 `careers: [{name, id, action: created|reused,
  stages_added}]` 与 `careers_assigned`。

---

## 场景：AI 单体生成与旧关系解析（任务 kind）

| kind | resource_key | 路由 | `done.result` |
|---|---|---|---|
| `character.generate` | 无 | `POST /projects/{pid}/characters/generate {ai_config_id?, instruction, role_type}` | `{draft, relation_suggestions, warnings}` |
| `organization.generate` | 无 | `POST /projects/{pid}/organizations/generate {ai_config_id?, instruction}` | `{draft, member_suggestions, warnings}` |
| `relations.parse` | `relparse:{character_id}` | `POST /projects/{pid}/characters/{cid}/parse-relations {ai_config_id?}` | `{character_id, character_name, suggestions, warnings}` |

- 实现：`entity_ai_service.py`（validate / build / run / normalize）+ `task_engine/handlers/entities.py`。**只产出草稿，不写设定数据，
  不写 generation_logs**；`on_abort` 无需还原。保存由前端调 `characters/with-relations`、`organizations/with-members`、
  `relationships/batch`（`source="ai_parse"`）。
- 关系建议（主体视角）：`{target_name, target_id, resolved, reason, existing_relationship_id, self_role, target_role,
  subject_is_a, type: {type_id|null, role_a, role_b, category}, intimacy, status, description}`。`subject_is_a` 为真时主体是 role_a 一方；
  `type_id` 为空表示保存时新建自定义类型。名字按「名字 → 别名」精确匹配；对不上 / 歧义 / 指向自己 / 已有关系 → `resolved=false` + `reason`。
- 成员建议：`{character_name, character_id, position, resolved, reason}`；组织草稿含 `parent_name` 与按名解析的 `parent_id`。
- `TaskOut.character_id`：`task_service.to_out` 取自 `input.request.character_id`（仅 `relations.parse` 有值，其余为 `null`），
  前端「解析旧关系」弹窗据此在 `GET /tasks?project_id&kind=relations.parse&active=true` 结果里接上本角色的活动任务。
- 创建前同步校验：项目归属、AI 配置、`relations.parse` 的角色归属（404）与旧备注非空（400 `该角色没有旧关系备注，无需解析`）。
- 提示词在 `app/ai/prompts/entities.py`：参考资料只给名字 / 定位 / 别名名单（≤3000 字）与世界观（≤4000 字），要求不重名。
- 测试：`tests/test_entity_tasks.py`。

---

## 场景：章节上下文组装（选角与裁剪，D4）

### 1. 范围

`context_builder.build_chapter_context(db, project, chapter, budget)`（签名不变，`generation_service` 不需要感知选角）、
`cast_service.select_cast`、`/chapters/{cid}/cast`。

### 2. 签名

```python
# cast_service
PROTAGONIST_ROLES = {"主角", "protagonist"}; PREV_TAIL_CHARS = 1500; MIN_TERM_LEN = 2
def is_protagonist(c) -> bool              # always_in_context or role_type.strip().lower() ∈ PROTAGONIST_ROLES
def match_characters(text, characters) -> list[int]   # 纯函数
async def select_cast(db, project, chapter, outline_summary, *, characters=None) -> Cast(involved, others)
# context_builder
@dataclass class CastSection: cards: list[str]; relations: list[RelationLine]; organizations: list[str]; roster: list[str]
def assemble_context(*, project_text, world_text, cast: CastSection | None, outline_text, prior_chapters, budget, ...) -> str
```

### 3. 契约

- 涉及角色 = 主角 ∪ `chapter_characters` 手动指定 ∪ 自动匹配（本章大纲梗概 + 上一章正文最后 1500 字），顺序按此分组、组内按角色排序。
- 自动匹配：名字与别名长度 < 2 跳过；按长度降序匹配，较短词只在未被较长词占用的位置出现才算命中（「林远山」不触发「林远」）；
  同一词属于多个角色时都命中。
- 输出段落：`【本章涉及角色】`（完整卡：别名、定位、非存活醒目标注【已死亡】、年龄性别、外貌、性格、背景、简介、当前状态、所属组织与职位；
  该角色在涉及角色间没有结构化关系时附 `关系备注：<relations_text>`）→ `【人物关系】`（仅涉及角色两两之间，
  `A 是 B 的师父（亲密度 80，正常）：描述`，对称为 `A 与 B 是朋友`）→ `【相关组织】`（`■ 名称（类型）：宗旨`）→
  `【其他角色（仅名单）】`（`名字（定位）`，中文逗号分隔）。世界观按分类分组（只有一个分类时不加 `〔分类〕` 标题）。
- 前情提要（更早章节，即最近 2 章全文之前的章节）：每章优先用**最近一次章节分析的 summary**（`context_builder.latest_summaries`，
  一条查询、子查询取每章 `max(id)`；正文已改、分析已过期照常使用；不截断），没有分析或摘要为空时退回正文前 200 字（`SUMMARY_CHARS`）。
  `PriorChapter.summary` 默认空串，`assemble_context` 签名不变。
- 裁剪顺序（文件头注释为准）：更早章节摘要 → 最近正文开头 → 世界观尾部 → **名单（从尾部逐个）→ 关系描述（从尾部逐条去掉）→
  关系（从尾部）→ 相关组织 → 涉及角色卡（尾部的角色先被截，主角在最前）** → 当前卷大纲 → 硬截断。

### 6. 必需的测试

`tests/test_cast_context.py`（匹配规则含「林 / 林远 / 林远山」、30 角色端到端提示词、裁剪顺序）、`tests/test_context_builder.py`
（原有预算用例改用 `CastSection`）。

### 7. 错误写法

```python
characters_text = story_service.format_characters(all_characters)   # 全部角色整段塞入，尾部角色被截掉
```

---

## 场景：向导适配（R11）

- 角色向导：新格式 `{characters: [结构化字段], relationships: [{a, b, role_a, role_b, category, intimacy, description}]}`；
  旧格式（数组 + `profile` / `relations_text`）照常解析，`relations_text` 保留为旧备注。先整体替换角色（级联清掉旧关系 / 成员 / 出场），
  再按名字或别名建关系（`apply_change(source="wizard")`），对不上 / 自指 / 同对已有的忽略。`done.result` 多一个 `relationships`
  （`RelationshipOut` 列表，仅 characters）。`always_in_context` 按定位推断。
- 世界观向导输出 `category`；`normalize_world_category` 把中英文映射到 `WORLD_CATEGORIES`，映射不了 → `other`。
- 测试：`tests/test_wizard.py::test_wizard_characters_structured_with_relations`、`test_wizard_world_categories`，原有旧格式用例保留。

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
  + 他们之间的关系（亲密度、状态、描述）+ 项目组织名 + 相关职业阶梯 + 正文 + 评分标准（1–3 / 4–5 / 6–7 / 8–9 / 10）+ JSON 结构；
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
