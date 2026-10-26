# 设计：职业等级体系

## 1. 边界

- 后端新增：
  - `career_service`：职业和阶段的 CRUD、挂接、`apply_assignment` 加历史。
  - `api/v1/careers.py`。
  - task kind `career.generate`。
  - wizard kind `wizard.careers`。
- 修改以下模块：
  - `context_builder`：角色卡加职业行，新增【职业体系】段。
  - `cast_service`：一并加载涉及角色的挂接。
  - `wizard_service`：增加职业步骤，角色向导支持分配职业。
  - `card_service`：卡片格式升到 v2。
  - `entity_ai_service`：生成角色时参考已有职业。

## 2. 数据模型（迁移 `0008_careers`）

> 开工时先确认最新编号。当前最新为 `0007_story_entities`；封面任务可能抢号，届时顺延。

### `careers`

`id, project_id CASCADE, name(≤100), kind(≤8: main/sub), description Text, requirements Text, abilities Text, rules Text, sort, created_at, updated_at`。`(project_id, name)` 唯一。

### `career_stages`

`id, career_id CASCADE, position int, name(≤50), description Text`。

- `(career_id, position)` 不建唯一约束，便于重排时整体更新；服务层保证 position 连续且从 1 开始。
- 同一职业内阶段名唯一（服务层校验），这是导入时按名称匹配的前提。

### `character_careers`（挂接）

`id, character_id CASCADE, career_id CASCADE, role(≤8: main/sub), stage_id FK career_stages, note(≤200), updated_at`。

- `(character_id, career_id)` 唯一。
- 每个角色最多 1 条 main、3 条 sub，由服务层校验。`role` 必须等于职业的 `kind`：主修职业只能挂成主职业，副修职业只能挂成副职业。
- `stage_id` 不设 ondelete。删除阶段前服务层先查引用，被引用时返回 409（R1）。删除职业时，阶段和挂接都随职业级联删除，同一条语句内完成，与 `relationships.type_id` 采用同样的处理方式。

### `career_history`

`id, character_id CASCADE, career_id FK careers SET NULL, career_name(≤100), source(≤16), chapter_no 可空, changes JSON, note(≤200), created_at`。

- `career_name` 冗余保存，职业被删后历史仍然可读。
- AC2 要求"删除职业后挂接和历史都被清理"，所以 `career_id` 改用 **CASCADE**，`career_name` 只用于展示。

## 3. 写入入口 `apply_assignment`

```python
SOURCES = ("manual", "wizard", "ai", "import", "analysis")
async def apply_assignment(db, character, career, *, stage_id=None, note=None, remove=False,
                           source, chapter_no=None, history_note="") -> CareerHistory | None   # 不 commit
```

- **新挂**：不传 `stage_id` 时默认第 1 阶。
- **校验**：职业属于同一项目；阶段属于该职业；不超过数量上限；`role` 由职业的 `kind` 决定。
- **更新**：对比 `stage_id` 和 `note`，有变化才写历史。`changes` 的格式为 `{"stage": ["第2阶·筑基", "第4阶·金丹"], "note": [旧, 新]}`，新挂时旧值为 null。
- **卸下**：`remove=True` 时删除挂接，并写一条 `changes={"removed": [职业名, null]}`。
- 所有写挂接的路径都只走这个入口：接口、向导、导入，以及以后的 analysis。这条约束写进 spec。

## 4. API（前缀 `/api/v1/projects/{pid}`，都先做归属校验，不通过返回 404）

| 端点 | 说明 |
|---|---|
| `GET/POST /careers`、`GET/PATCH/DELETE /careers/{id}` | `CareerOut` 包含 `stages: [{id, position, name, description}]` 和 `holder_count`。POST 和 PATCH 可以整体提交 `stages`：带 id 的更新，不带 id 的新建，缺失的删除（被引用时返回 409），提交顺序就是新的 position |
| `GET /careers/{id}/holders` | `[{character_id, character_name, role, stage_id, stage_name, stage_position, note}]` |
| `POST /careers/batch {careers: [...]}` | AI 预览后一次性保存，单个事务 |
| `GET /characters/{cid}/careers` | 该角色的挂接列表，包含职业名、阶段名、`position/total` |
| `POST /characters/{cid}/careers {career_id, stage_id?, note?}` | 新挂 |
| `PATCH /characters/{cid}/careers/{aid} {stage_id?, note?, chapter_no?, history_note?}` | 换阶或改备注 |
| `DELETE /characters/{cid}/careers/{aid}` | 卸下（写历史） |
| `GET /characters/{cid}/career-history` | 倒序 |
| `POST /careers/generate {ai_config_id?, count(1–5), kind?, instruction}` | 返回 202 `TaskOut`，kind 为 `career.generate` |

错误文案：

- 409「同名职业已存在」
- 409「该阶段仍有角色处于其中，无法删除」
- 400「职业至少需要 1 个阶段」
- 400「同一职业内阶段名称不能重复」
- 409「该角色已有主职业」
- 409「副职业最多 3 个」
- 409「该角色已挂该职业」
- 400「主修职业只能作为主职业」/「副修职业只能作为副职业」
- 400「阶段不属于该职业」
- 400「职业不存在或不属于该项目」

## 5. AI 与向导

### `career.generate`

- 没有 `resource_key`。
- 提示词新建在 `ai/prompts/careers.py`，参考项目信息、世界观、已有职业（只给名称、类别和阶梯摘要），要求输出 JSON：`{careers: [{name, kind, description, requirements, abilities, rules, stages: [{name, description}]}]}`。
- 结果格式为 `{drafts: [... , conflict: bool]}`：与已有职业同名的标记 `conflict=true`，前端置灰，需要改名后才能勾选。
- 不写库，也不写生成记录，和其他实体任务一致。

### `wizard.careers`

- `resource_key` 为 `wizard:{pid}:careers`，与其他向导互斥。
- 这一步是整体覆盖：删除全部职业，级联清掉挂接和历史，再写入新职业。
- 结果格式为 `{kind: "careers", items: CareerOut[]}`。

### 角色向导分配职业

- 项目已有职业时，提示词附上职业和阶梯，要求每个角色输出 `careers: [{name, stage, role, note}]`。
- 写入时按职业名和阶段名匹配，通过 `apply_assignment(source="wizard")` 写入。
- 匹配不上或超出上限的，忽略并追加到 `done.result.warnings`，前端展示这些警告。
- 项目没有职业时，提示词不变（AC6）。

### 生成单个角色（`character.generate`）

- 有职业时同样参考职业列表，草稿里增加 `career_suggestions`，在预览中可以勾选。
- `characters/with-relations` 的请求体增加可选的 `careers` 字段。

## 6. 上下文注入（R7）

- `cast_service`：涉及角色的挂接一次性查出，避免 N+1 查询。
- 角色卡增加一行：`职业：剑修·筑基（2/9，瓶颈期）；副职业：丹师·一品（1/9）`。
- 新增段落【职业体系】：对涉及角色所挂的每个职业输出 `■ 剑修（主修）：练气 → 筑基 → 金丹 → …`。如果规则文本非空，附一行"规则：…"，截取前 200 字。
- 裁剪顺序：在原顺序中"截断组织"之前插入"截断职业体系"，并更新 `context_builder` 的文件头注释。

## 7. 卡片 v2（R9）

- 角色项增加 `careers: [{career_name, role, stage_name, note}]`，顶层增加 `careers: [{name, kind, description, requirements, abilities, rules, stages: [{name, description}]}]`，只包含所涉及的职业。
- 解析时接受 v1 和 v2；v1 卡片里没有职业，按原逻辑导入。
- 导入流程：
  1. 职业按名称匹配，匹配上就复用，匹配不上就新建。
  2. 阶段按名称匹配，匹配不上就追加到阶梯末尾。
  3. 挂接通过 `apply_assignment(source="import")` 写入；超出上限或类别不符的跳过，并给出警告。
- 预览结果增加 `careers: [{name, status: new|reuse}]`。

## 8. 前端

- **职业页**：NAV 新增「职业」，对应 `CareersPanel`，走路由懒加载。
  - 列表按主修、副修分组，每张卡片显示阶梯（Steps 或标签串）和持有人数。
  - 编辑抽屉：基础信息，加上阶段列表（拖拽排序可以用 antd 的 `Table` 拖拽或上下移动按钮，不引入新依赖），以及持有人列表。
- **AI 生成职业**：沿用 `EntityTaskModal` 的模式，并在 `taskResultViewers` 注册 `career.generate`。
- **角色抽屉的「职业」Tab**：挂接列表，可选职业和阶段、编辑备注、卸下；每一条可以展开查看历史。
- **角色卡片**：显示主职业·阶段。
- **向导**：`STEPS` 在世界观之后插入 `{kind: 'careers', title: '职业体系'}`，支持「跳过」。角色步骤展示 `warnings`。`StoryPreview` 适配职业结果的展示。
- **卡片导入预览**：显示职业的新建或复用情况。
- 补齐 `TaskKind`/标签：`career.generate`、`wizard.careers`。

## 9. 兼容、风险与回滚

- 迁移只新增 4 张表，降级时删除。迁移测试按指定版本号执行。
- 卡片 v1 必须仍能导入，原有卡片测试保留，另外补 v2 测试。
- 职业步骤覆盖时会清掉挂接，前端在执行前用文案提示。
- 职业名匹配用 strip 后的全等比较，不做模糊匹配。匹配不上时明确给出警告，不像 MuMu 那样静默丢失。
- 回滚：回退代码后执行 `alembic downgrade -1`。
