# 设计：结构化设定体系

## 1. 边界

- 后端新增以下服务和路由：
  - `organization_service`、`relationship_service`（包含关系类型和历史）
  - `card_service`（导入导出）
  - `cast_service`（选角）
  - `api/v1/organizations.py`、`relationships.py`、`cards.py`
- 角色和世界观继续使用 `story_service` 的通用 CRUD，只扩展 schema；角色删除改为专用实现，以便清理关联数据（见 §2.6）。
- AI 生成通过 `task_engine` 注册三个新 kind：`character.generate`、`organization.generate`、`relations.parse`。
- 本任务提供 `relationship_service.apply_change(..., source, chapter_no)` 这样的写入接口，`analysis-memory` 以后直接复用。

## 2. 数据模型（迁移 `0007_story_entities`）

> 开工时先确认 `backend/alembic/versions` 的最新编号（当前是 `0006_tasks`），封面任务可能抢先占用编号。

### 2.1 `characters` 增加的列

| 列 | 类型 | 说明 |
|---|---|---|
| aliases | JSON，默认 `[]` | 别名列表，单个别名 ≤50 字，最多 10 个 |
| age / gender | String(50) / String(20)，默认 "" | |
| appearance / personality / background | Text，默认 "" | |
| current_state | Text，默认 "" | 当前处境；以后由 analysis-memory 回写 |
| life_status | String(16)，默认 `alive` | `alive`、`dead`、`missing`、`other` |
| always_in_context | bool，默认 false | 迁移时，`role_type` 为"主角"或 `protagonist` 的角色回填为 true |

`role_type` 保持 String(50)。前端用下拉框，选项为主角、重要配角、配角、反派、其他，同时兼容旧的自由文本值：不在选项里的原值照常显示，并允许保留。判断是否为主角统一用 `cast_service.is_protagonist`，规则是 `always_in_context` 为真，或 `role_type ∈ {主角, protagonist}`。

`profile` 和 `relations_text` 保持不动（D3）。UI 上的标签分别改为"简介"和"旧关系备注（只读）"。

### 2.2 `world_settings.category`

String(16)，默认 `other`。取值为 `place`、`faction`、`item`、`rule`、`history`、`other`，对应中文标签地点、势力、物品、规则、历史、其他。存量数据回填为 `other`。

### 2.3 组织

**`organizations`**

- 字段：`id, project_id CASCADE, name(≤100), org_type(≤50), purpose Text, location(≤200), power_level int 可空(0–100), parent_id FK organizations SET NULL, description Text, sort`。
- `(project_id, name)` 唯一。
- 设置 `parent_id` 时，服务层沿上级链向上查，最多 50 层，出现自身即判为循环，返回 400。`parent_id` 必须属于同一项目。

**`organization_members`**

- 字段：`id, organization_id CASCADE, character_id CASCADE, position(≤50), status(≤16: active/left/expelled/dead), note Text`。
- `(organization_id, character_id)` 唯一。
- 角色和组织必须属于同一项目。

### 2.4 关系（D2）

**`relationship_types`**

- 字段：`id, project_id CASCADE 可空, category(≤16: family/social/hostile/work/other), role_a(≤20), role_b(≤20), sort`。
- `project_id` 为空的是全局预置类型。约 20 种预置类型由迁移插入，示例：
  - family：父亲/子女、母亲/子女、兄长/弟妹、配偶/配偶
  - social：师父/徒弟、朋友/朋友、知己/知己、恋人/恋人、同门/同门
  - hostile：仇人/仇人、宿敌/宿敌、竞争对手/竞争对手
  - work：上司/下属、同僚/同僚、合作者/合作者、主人/仆从
- 自定义类型挂在项目下，按 `(project_id, role_a, role_b)` 去重。
- 预置类型只读，接口不允许修改或删除。

**`relationships`**

- 字段：`id, project_id CASCADE, a_id FK characters CASCADE, b_id FK characters CASCADE, pair_low int, pair_high int, type_id FK relationship_types RESTRICT, intimacy int(-100–100，默认 0), status(≤16: normal/broken/past/complicated), description Text, created_at, updated_at`。
- 语义："a 是 b 的 `role_a`，b 是 a 的 `role_b`"。从 A 的视角，对方显示为 `role_b`，即"徒弟 B"；从 B 的视角，对方显示为 `role_a`，即"师父 A"。
- `pair_low = min(a_id, b_id)`、`pair_high = max(...)`，`(pair_low, pair_high)` 唯一，保证一对角色只有一条关系。`a_id ≠ b_id`。这两列由服务层写入，不暴露给接口。
- 删除仍被引用的自定义类型时返回 409。

**`relationship_history`**

- 字段：`id, relationship_id CASCADE, chapter_no int 可空, source(≤16: manual/wizard/ai_parse/import/analysis), changes JSON {字段: [旧, 新]}, note(≤200), created_at`。
- 新建关系时也写一条历史，`changes` 记录所有初始值。

`relationship_service.apply_change(db, rel, values, *, source, chapter_no=None, note="")` 是唯一的写入入口：它对比旧值和新值，有变化时更新并写历史，不自行 commit。手动编辑、导入、AI 解析、向导，以及以后的 analysis-memory，都通过它写入。

### 2.5 章节出场角色

`chapter_characters(chapter_id CASCADE, character_id CASCADE)`，复合主键。
- `PUT /projects/{pid}/chapters/{cid}/cast {character_ids}` 整体替换。
- `GET` 返回 id 列表。
- 所有 id 必须属于本项目。

### 2.6 删除

外键已全部设为 CASCADE，删除角色会自动清理关系、历史、成员关系和出场记录。SQLite 测试已开启 `PRAGMA foreign_keys`。角色删除仍走通用 CRUD，测试中断言关联数据已被清理。

## 3. API（都在 `/api/v1/projects/{pid}` 下，先做归属校验，不通过返回 404）

| 资源 | 端点 |
|---|---|
| 角色 | 沿用 `/characters` CRUD，schema 扩展；新增 `POST /characters/with-relations`：创建一个角色，并同时创建勾选的关系，单事务 |
| 世界观 | 沿用 `/world` CRUD，加 `category`；列表支持 `?category=` |
| 组织 | `/organizations` CRUD；`GET/POST /organizations/{oid}/members`、`PATCH/DELETE /organizations/{oid}/members/{mid}` |
| 关系类型 | `GET /relationship-types`（预置加本项目自定义）、`POST`、`DELETE /{tid}` |
| 关系 | `GET /relationships?character_id=`，返回时附带双方姓名和类型称呼；`POST`、`PATCH /{rid}`、`DELETE /{rid}`、`GET /{rid}/history`；`GET /relationship-graph` 返回 `{nodes, edges, organizations}` |
| 出场角色 | `GET/PUT /chapters/{cid}/cast` |
| 卡片 | `POST /cards/export {character_ids, organization_ids}` 返回 JSON；`POST /cards/import/preview {data}`；`POST /cards/import {data, resolutions}` |
| AI 任务 | `POST /characters/generate {ai_config_id?, instruction}`、`POST /organizations/generate {...}`、`POST /characters/{cid}/parse-relations {ai_config_id?}`，都返回 202 `TaskOut` |

## 4. AI 任务（task_engine handlers）

| kind | resource_key | 输入 | `done.result` |
|---|---|---|---|
| `character.generate` | 无，允许并发 | instruction、role_type 可选 | `{draft: {角色字段}, relation_suggestions: [{target_name, target_id, type: {role_a, role_b, type_id?}, intimacy, description, resolved}]}` |
| `organization.generate` | 无 | instruction | `{draft: {组织字段}, member_suggestions: [{character_name, character_id, position, resolved}]}` |
| `relations.parse` | `relparse:{character_id}` | character_id | `{suggestions: [...同上，从该角色视角...]}` |

- 提示词新建在 `ai/prompts/entities.py`，要求输出 JSON，用现有的 `extract_json` 加 normalize 容错。参考资料包括项目信息、按分类组织的世界观、已有角色和组织名单（只给名字、定位和别名，控制长度），并明确要求不要和已有名字重复。
- 名字解析：按名字或别名精确匹配已有角色。匹配不上时 `resolved=false`，前端标红，不能勾选。
- 任务只产出草稿，不写库。保存由前端预览后调用 `with-relations`、组织的 CRUD 加成员接口、关系的 POST 完成（R6/R7）。
- 不写 generation_logs（与向导一致；生成记录的 kind 只覆盖章节类操作）。

## 5. 卡片格式（R8）

```json
{
  "format": "ainovel.cards",
  "version": 1,
  "exported_at": "...",
  "characters": [{"name", "aliases", "role_type", "age", "gender", "appearance", "personality", "background", "profile", "current_state", "life_status"}],
  "organizations": [{"name", "org_type", "purpose", "location", "power_level", "description", "parent_name", "members": [{"character_name", "position", "status", "note"}]}],
  "relationships": [{"a_name", "b_name", "role_a", "role_b", "category", "intimacy", "status", "description"}]
}
```

- 导出范围：勾选的角色和组织，加上关系两端都在导出集合内的关系，以及成员角色都在集合内的成员关系。任何地方都不出现内部 ID。
- **预览**：校验 `format`、`version`，数据大小上限 2MB（前端读取文件后作为 JSON body 提交，不需要 multipart 依赖）。返回 `{characters: [{name, status: new|conflict}], organizations: [...], relationships: [{a_name, b_name, status: ok|unresolved}], warnings}`。
- **导入**：`resolutions` 为 `{characters: {name: "skip"|"rename"}, organizations: {...}}`。选择 rename 时，自动追加"（导入）""（导入2）"……直到不冲突。
  - 导入后建立"导入名 → 新 id"的映射。关系和成员关系的两端先在映射里找；被跳过的同名项，映射到目标项目里已有的同名对象。
  - 关系类型按 `(role_a, role_b)` 匹配预置类型或本项目的自定义类型，匹配不上就新建自定义类型。
  - 关系一律通过 `apply_change(source="import")` 写入；同一对角色已有关系时，跳过该条并记入 warnings。
  - 整个导入在一个事务内完成。

## 6. 选角与上下文注入（D4 / R10）

`cast_service.select_cast(db, project, chapter, outline_summary) -> Cast(involved_ids, others)`：

1. 主角：`is_protagonist` 为真的角色。
2. `chapter_characters` 里手动指定的角色。
3. 自动匹配：匹配文本是本章梗概加上上一章正文的最后约 1500 字。对每个角色的名字和别名做子串匹配。长度小于 2 的名字或别名跳过，避免单字误命中；存在包含关系时，比如"林"和"林远"，只要较长的那个命中，就不再单独计较短的。

`context_builder` 的角色部分重写：

- **涉及角色卡**：名字（附别名）、定位、年龄和性别、外貌、性格、背景、当前状态、存活状态（非存活时醒目标注），以及所属组织和职位。
- **关系**：只列涉及角色两两之间的关系，格式为"A 是 B 的师父（亲密度 80，正常）：描述"。
- **组织**：涉及角色所属的组织，列出名称、类型和宗旨。
- **名单**：其余角色只列"名字（定位）"，用逗号分隔。
- **世界观**：按分类分组输出。
- **裁剪顺序**（D4）：先在原有顺序中插入角色相关的步骤：先截断名单，再截断关系描述，再按序截断组织和涉及角色卡。原有的前情、正文、世界观裁剪顺序保持不变，整体顺序在 `context_builder` 文件头注释里写清楚。

`build_chapter_context` 的签名不变，选角在内部完成，`generation_service` 不需要改动。

## 7. 向导适配（R11）

- 角色向导的提示词要求输出 `{characters: [...结构化字段], relationships: [{a, b, role_a, role_b, category, intimacy, description}]}`。`normalize_characters` 同时兼容旧格式：只有 `profile` 也能解析，关系部分可选。
- 写入顺序：先整体替换角色（级联会清掉旧关系），再按名字建立关系，通过 `apply_change(source="wizard")` 写入；名字对不上的关系忽略。
- 世界观向导的提示词要求输出 `category`，normalize 时把中英文都映射到六个枚举值，映射不了的归为 `other`。
- 由于 `done.result.items` 的内容变了，前端 `StoryPreview` 要同步调整展示。

## 8. 前端

- **路由和导航**：`ProjectLayout` 的 `NAV` 新增"组织"和"关系"两项；关系图谱作为"关系"页里的一个 Tab，或者单独路由，用 `React.lazy` 加载。
- **依赖**：`@xyflow/react`（MIT）加 `@dagrejs/dagre`（MIT），只在图谱 chunk 里引用。构建后核对主 chunk 小于 1500 kB；图谱 chunk 单独列出大小。
- **CharactersPanel 重做**：
  - 卡片列表支持按定位筛选。
  - 编辑抽屉分 Tab：基础（名字、别名、定位、年龄、性别、存活状态、始终注入上下文）、描写（外貌、性格、背景、简介）、状态（当前状态）、关系（该角色视角的关系列表，可增删改，可看历史）、组织（所属组织和职位）。
  - "旧关系备注"以只读块展示，配一个"AI 解析为关系"按钮。
  - 工具栏有："AI 生成角色"、"导出卡片"、"导入卡片"、批量勾选。
- **OrganizationsPanel（新）**：左边是组织树（按 `parent_id`），右边是详情和成员表。提供"AI 生成组织"。
- **RelationshipsPanel（新）**：
  - "列表" Tab：表格加筛选。
  - "图谱" Tab：懒加载。节点按定位着色，边的标签显示称呼，边的颜色按亲密度从红到灰到绿渐变；可按组织或类别筛选，点击节点弹出角色卡。
- **WorldPanel**：按分类分组，编辑时可选分类。
- **AI 预览**：
  - 生成任务在任务面板里显示，用 `useTaskStream` 订阅；完成后弹出预览。
  - 角色或组织的草稿可以编辑，关系和成员建议以复选列表展示，`resolved=false` 的置灰并标出原因。确认后调用保存接口。
  - 如果关闭了页面，在任务面板点"查看"时能打开同一个预览：从 `GET /tasks/{id}` 和 result 读取。任务面板因此需要支持按 kind 打开对应的结果视图，这一点对 `TaskPanel` 来说是新增扩展点。
- **卡片导入**：选择文件后在本地读成 JSON，先调预览接口，在弹窗里按冲突逐项选择跳过或重命名，再确认导入。
- **章节编辑器**：标题栏新增"出场角色"多选（`Select mode=multiple`），保存时调用 `PUT cast`。这和正文保存分开，不影响 dirty 判断。

## 9. 兼容、风险与回滚

- **迁移**：只新增表和列，加回填（`world_settings.category`、`characters.always_in_context`）和预置类型的插入。降级时删掉这些表和列。在 `tests/test_migrations.py` 中按指定版本号测试回填结果。
- **向导输出格式变化**：旧格式仍然兼容，FakeLLM 的测试数据两种格式都要覆盖。
- **自动匹配误命中**：用最短 2 字和长名优先来缓解，并配上中文同名子串的测试。
- **任务面板扩展点**：做成"kind → 结果查看组件"的注册表，避免 `TaskPanel` 里硬编码。
- **回滚**：回退代码，再执行 `alembic downgrade -1`。
