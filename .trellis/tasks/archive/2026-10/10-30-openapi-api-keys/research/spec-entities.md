# 本任务规范摘录：.trellis/spec/backend/story-entities.md

为避免上下文注入截断，以下从当前原规范逐字摘录本任务相关段落。原文件是权威来源；若实施涉及摘录外内容，直接读取原规范相关部分。

- 提取日期：2026-10-30
- 来源 SHA-256：`f7b532fff1d1cbef2db421cbab8abf1c13b961f5200bd2ce900bd78ef4ebc561`

## 原文 .trellis/spec/backend/story-entities.md:1–84

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
