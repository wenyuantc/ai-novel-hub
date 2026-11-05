# 任务上下文摘录：entities-core

来源：`.trellis/spec/backend/story-entities.md`，原文第1–205行。
源文件SHA256：`f7b532fff1d1cbef2db421cbab8abf1c13b961f5200bd2ce900bd78ef4ebc561`。
这是2026-11-03为避免自动注入截断而保留的连续原文，不修改或降低原规范；涉及摘录外主题时按原索引读取相应章节。若原规范更新，以原文件为准。

---

# 结构化设定：角色 / 组织 / 关系 / 卡片 / 章节上下文

> 来源任务：`10-23-story-entities`；职业等级体系来自 `10-23-career-system`；章节分析与回写来自 `10-23-analysis-memory`；伏笔管理（文末）来自 `10-26-foreshadows`。表结构与外键规则见 [database-guidelines.md](./database-guidelines.md)，
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
- 约束竞争可能在 flush 而非 commit 抛出：`apply_change` 只精确翻译角色对冲突、不主动 rollback；`resolve_type` 的类型首次创建竞争给独立中文409重试提示。HTTP请求或原有外层SAVEPOINT负责回滚，不误报无关完整性错误。详见[批量关系生成](./relationship-generation.md)。
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
| `relations.generate` | 无 | `POST /projects/{pid}/relationships/generate {character_ids, count?, instruction?, ai_config_id?}` | `{character_ids, suggestions, warnings}` |

- 实现：`entity_ai_service.py`（validate / build / run / normalize）+ `task_engine/handlers/entities.py`。**只产出草稿，不写设定数据，
  不写 generation_logs**；`on_abort` 无需还原。保存由前端调 `characters/with-relations`、`organizations/with-members`、
  `relationships/batch`（旧解析 `source="ai_parse"`；批量生成 `source="ai_generate"`）。
- `relations.generate` 只在所选已有角色间补充缺失关系，按 ID 关联；其请求/上下文/结果、只读草稿、并发与前端恢复详见[完整跨层契约](./relationship-generation.md)，不使用下文旧解析的名字匹配。
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

- 涉及角色 = 主角 ∪ `chapter_characters` 手动指定 ∪ 自动匹配（本章任务梗概 + 关键事件 + 上一章正文最后 1500 字），顺序按此分组、组内按角色排序。
- 本章任务（`10-23-outline-pro`）：`context_builder.chapter_task` 优先取章节细纲（梗概、关键事件、叙事目标、情感基调），否则取大纲梗概；
  见 [outline.md](./outline.md)「本章任务优先细纲」。
- 自动匹配：名字与别名长度 < 2 跳过；按长度降序匹配，较短词只在未被较长词占用的位置出现才算命中（「林远山」不触发「林远」）；
  同一词属于多个角色时都命中。
- 输出段落：`【本章涉及角色】`（完整卡：别名、定位、非存活醒目标注【已死亡】、年龄性别、外貌、性格、背景、简介、当前状态、所属组织与职位；
  该角色在涉及角色间没有结构化关系时附 `关系备注：<relations_text>`）→ `【人物关系】`（仅涉及角色两两之间，
  `A 是 B 的师父（亲密度 80，正常）：描述`，对称为 `A 与 B 是朋友`）→ `【相关组织】`（`■ 名称（类型）：宗旨`）→
  `【其他角色（仅名单）】`（`名字（定位）`，中文逗号分隔）。世界观按分类分组（只有一个分类时不加 `〔分类〕` 标题）。
- 前情提要（更早章节，即最近 2 章全文之前的章节）：每章优先用**最近一次章节分析的 summary**（`context_builder.latest_summaries`，
  一条查询、子查询取每章 `max(id)`；正文已改、分析已过期照常使用；不截断），没有分析或摘要为空时退回正文前 200 字（`SUMMARY_CHARS`）。
  `PriorChapter.summary` 默认空串，`assemble_context` 签名不变。
- `【伏笔提醒】`（`10-26-foreshadows`，见文末「伏笔管理」）位于【当前卷大纲】之后、【前情提要】之前；`assemble_context(..., foreshadows=ForeshadowSection | None)`
  可选参数，没有可提醒的伏笔时整段不出现、上下文与之前逐字一致。
- 检索块（`10-23-knowledge-base`，见 [retrieval.md](./retrieval.md)）：`build_chapter_context(..., *, embedder=None)`（可选关键字参数，
  generate / revise / 批量传入当前用户的向量模型）；`assemble_context(..., knowledge: str = "", memories: MemorySection | None = None)`。
  `【参考资料（来自知识库，仅供参考；与项目设定冲突时以设定为准）】` 位于【世界观】之后；`【相关旧情节】` 位于【前情提要】之后、【最近章节正文】之前，
  条目已按章号排列（`MemoryLine(text, pinned, rank)`）。查询 = 章节标题 + 梗概 + 关键事件 + 涉及角色名（`CastSection.names`，不进上下文；
  记忆另加叙事目标）。`prior_coverage(prior)` 给出（全文章节号, 有分析摘要的更早章节号），按裁剪前划分传给 `memory_service.recall`。
  两块都为空时上下文与之前逐字一致；检索出错时对应块为空、不阻断生成。
- 裁剪顺序（文件头注释为准）：**参考资料尾部 → 相关旧情节中未钉住的条目（rank 大 = 最不相关的先丢）** → 更早章节摘要 → 最近正文开头 → 世界观尾部 → **伏笔提醒整档去掉「长线」→ 再整档去掉「即将到期」** → **名单（从尾部逐个）→ 关系描述（从尾部逐条去掉）→
  关系（从尾部）→ 相关组织 → 涉及角色卡（尾部的角色先被截，主角在最前）** → 当前卷大纲 → 硬截断。

### 6. 必需的测试

`tests/test_cast_context.py`（匹配规则含「林 / 林远 / 林远山」、30 角色端到端提示词、裁剪顺序）、`tests/test_context_builder.py`
（原有预算用例改用 `CastSection`；伏笔提醒位置 / 五档顺序 / 裁剪顺序 / 无伏笔不变 / 端到端第 8 章提示词；检索块为空不变 / 位置 /
先裁参考资料再丢未钉住旧情节 / `prior_coverage`）、`tests/test_memories.py`（端到端召回范围与位置）。

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
- 大纲向导：`outline_mode` replace（默认，已有大纲时先存快照）/ append（接在末尾），见 [outline.md](./outline.md)「向导的大纲模式与进度」。
- 世界观向导输出 `category`；`normalize_world_category` 把中英文映射到 `WORLD_CATEGORIES`，映射不了 → `other`。
- 测试：`tests/test_wizard.py::test_wizard_characters_structured_with_relations`、`test_wizard_world_categories`，原有旧格式用例保留。

---

