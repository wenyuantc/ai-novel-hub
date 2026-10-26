# 设计：伏笔管理

## 1. 边界

- **新增模块**
  - `foreshadow_service`：伏笔 CRUD、状态流转、统计、时间线数据、上下文提醒选取
  - `api/v1/foreshadows.py`
  - 统一写入入口 `apply_foreshadow`，所有伏笔写入都走它（模式与 `apply_change`、`apply_assignment` 一致）
- **修改的现有模块**
  - `analysis_service`：分析输入里附上伏笔清单；把伏笔候选转成回写条目
  - `writeback_service`：新增 3 种条目类型（kind）
  - `ai/prompts/analysis.py`：增加伏笔参数和输出说明
  - `context_builder`：新增【伏笔提醒】段

## 2. 数据模型（迁移 `0010_foreshadows`）

> 开工时确认编号。当前最新为 `0009_chapter_analysis`，封面任务如果先占了编号就顺延。

### `foreshadows`

| 字段 | 说明 |
|---|---|
| id, project_id CASCADE（index） | |
| title String(200)、content Text | |
| plant_hint Text、reveal_hint Text | 埋法要点、回收要点 |
| status String(16) | `pending`、`planted`、`resolved`、`partial`、`abandoned` |
| planned_plant_chapter、actual_plant_chapter、planned_resolve_chapter、actual_resolve_chapter | 都是 int、可空，存**章节号**，不做外键。章节号可以指向尚未创建的章节，且重排章节时不受影响 |
| importance int（1–5，默认 3） | |
| include_in_context bool（默认 true） | |
| note Text | |
| source String(12) | `manual` 或 `analysis`（确认创建时写 analysis） |
| created_at / updated_at | |

### `foreshadow_characters`

`(foreshadow_id CASCADE, character_id CASCADE)`，复合主键。删除角色时只删除关联行，满足 AC1。

### `foreshadow_history`

`id, foreshadow_id CASCADE, source(manual/analysis), chapter_no 可空, changes JSON {字段: [旧, 新]}, note, created_at`。撤销和审计都依赖这张表。

### 派生属性（不入库）

- `is_long_term`：status ∈ {planted, partial} 且 `planned_resolve_chapter` 为空。
- `overdue_by(current_no)`：status ∈ {planted, partial}、`planned_resolve_chapter` 不为空且小于 current_no 时，等于 `current_no - planned_resolve_chapter`。列表页的 `current_no` 取项目中已有正文的最大章节号。

## 3. 写入入口 `apply_foreshadow`

```python
async def apply_foreshadow(db, fs, values, *, source, chapter_no=None, note="") -> ForeshadowHistory | None  # 不 commit
```

- 只有值发生变化时才写历史，追踪字段为 status 和四个章节字段，以及标题等文本字段。
- 状态流转不设硬性限制（R1 允许手动改回），但所有写入都必须经过这个入口。
- 标为已埋时，如果 `actual_plant_chapter` 为空就要求传入；标为已回收或部分回收时同理要求 `actual_resolve_chapter`。缺失时报 400「请填写实际埋设章节」/「请填写实际回收章节」。

## 4. 分析联动（D1 / R2）

### 输入

`analysis_service` 调用 `build_analysis_messages(..., foreshadows_text=...)`，选取以下两类伏笔：

- status ∈ {planted, partial} 的全部伏笔；
- status = pending 且 `planned_plant_chapter <= 本章`（或为空）的伏笔。

伏笔多时按重要度和计划回收章排序，最多取 30 条。格式为 `#ID 标题｜内容摘要（≤60 字）｜状态｜计划回收第N章`。提示词新增规则：埋下或回收清单中的伏笔时，必须在 `ref_id` 填写编号；新伏笔不填 `ref_id`。

### 输出

`foreshadows: [{action: plant|resolve, ref_id?, text, keyword, expected_resolve_chapter?, partial?: bool}]`。规范化时只保留整数 `ref_id`。

### 转成回写条目

`writeback_service` 新增三种 kind：

| kind | 条件 | 处理 | `field_key` |
|---|---|---|---|
| `foreshadow_resolve` | `action=resolve` 且 `ref_id` 指向本项目的已埋或部分回收伏笔 | 自动写入：status 改为 resolved（`partial=true` 时改为 partial），`actual_resolve_chapter` 设为本章 | `fs:{id}:resolve` |
| `foreshadow_plant_planned` | `action=plant` 且 `ref_id` 指向本项目的待埋伏笔 | 自动写入：status 改为 planted，`actual_plant_chapter` 设为本章 | `fs:{id}:plant` |
| `foreshadow_new` | `action=plant` 且没有 `ref_id` | **待确认**；after 为 `{title, content, plant_hint, planned_resolve_chapter, importance: 3}`，title 取 keyword 或 text 前 20 字 | 空串（无需"更晚章节优先"判定） |

- `ref_id` 无效（不存在、属于其他项目、状态不符）时，记为 `skipped`，reason 写「伏笔 #N 不存在或状态不符」。`action=resolve` 但没有 `ref_id` 时同样记为 `skipped`，reason 写「未引用伏笔编号，无法回收」，只做提示。
- **更晚章节优先**：沿用现有的 `field_key` 机制。同一伏笔如果已经被更晚的章节回收，本条记为 skipped。
- **撤销**：只有当前值等于 after 时，才通过 `apply_foreshadow` 写回 before；否则返回 409（沿用现有文案）。
- **确认 `foreshadow_new`**：`POST /analysis-changes/{id}/confirm` 增加可选请求体 `{overrides: {title?, content?, plant_hint?, reveal_hint?, planned_resolve_chapter?, importance?, character_ids?}}`，只对这个 kind 生效，其他 kind 传了会被忽略。
  - 创建时 status 为 planted，`actual_plant_chapter` 为本章，source 为 analysis。
  - 同项目、同一 `actual_plant_chapter`、同标题（strip 后全等）的伏笔已存在时，返回 409「本章已有同名伏笔」。
  - 确认后 `target` 补上 `foreshadow_id`。撤销已确认的条目等于删除该伏笔；如果伏笔已经被修改，返回 409。

## 5. 上下文提醒（D2 / R3）

`foreshadow_service.reminders(db, project_id, chapter_no) -> ForeshadowReminders`：

- `to_plant`：pending 且 `planned_plant_chapter == N`，全部列出。
- `must_resolve`：planted 或 partial，且 `planned_resolve_chapter == N`，全部列出。
- `overdue`：planted 或 partial，且 `planned_resolve_chapter < N`，按超期章数倒序，最多 3 条。
- `upcoming`：planted 或 partial，且 `N < planned_resolve_chapter <= N+3`，最多 3 条。
- `long_term`：planted 或 partial，且没有计划回收章，按重要度倒序、id 正序，最多 3 条。
- 以上都只取 `include_in_context=true` 的伏笔。

渲染为：

```
【伏笔提醒】
◆ 本章计划埋设：#12 玉佩来历 —— 埋法：在打斗间隙让主角瞥见…
◆ 本章必须回收：#5 师父的旧伤 —— 回收：揭示旧伤来自…
◆ 超期未回收：#3 神秘信件（已超期 2 章）
◆ 即将到期（可适当铺垫）：#7 …（第 10 章）
◆ 长线伏笔：#2 血脉之秘
```

关联角色以「（关联：林远、张三）」的形式附在每条后面。

- **位置**：段落放在"前情提要"之前。
- **裁剪**：在 `context_builder` 原有的裁剪顺序中插入两步：先整档去掉"长线伏笔"，再去掉"即将到期"，这两步排在"截断名单"之前。其余三档（本章计划埋设、本章必须回收、超期未回收）跟其他段落一起，在最后的硬截断里处理。文件头注释要同步更新。

## 6. API（前缀 `/api/v1/projects/{pid}`，都先校验归属，不通过返回 404）

| 端点 | 说明 |
|---|---|
| `GET /foreshadows?status=&overdue=&long_term=&character_id=&sort=planned_resolve\|importance` | 列表，`ForeshadowOut` 带派生属性 `is_long_term`、`overdue_by` 和 `character_ids/names` |
| `POST /foreshadows`、`GET/PATCH/DELETE /foreshadows/{id}` | 写入一律经过 `apply_foreshadow`；PATCH 可以带 `character_ids`（整体替换），也可以带 `chapter_no`、`note` 进历史 |
| `POST /foreshadows/{id}/plant {chapter_no}`、`/resolve {chapter_no, partial?}`、`/abandon` | 快捷操作 |
| `GET /foreshadows/{id}/history` | |
| `GET /foreshadows/stats` | `{pending, planted, resolved, partial, abandoned, overdue, long_term, current_chapter}` |
| `GET /chapters/{cid}/foreshadow-reminders` | 编辑器"本章伏笔"入口用，与上下文提醒同源 |
| `POST /analysis-changes/{id}/confirm` | 扩展可选 overrides（见 §4） |

错误文案：
- 400「关联角色不存在或不属于该项目」
- 400「请填写实际埋设章节」「请填写实际回收章节」
- 409「本章已有同名伏笔」
- 404「伏笔不存在」

## 7. 前端

- **伏笔页 `ForeshadowsPanel`**（懒加载路由，导航新增"伏笔"）：
  - 顶部统计卡，含超期数（红色）。
  - 筛选栏：状态、仅超期、仅长线、关联角色；排序方式可选。
  - 列表行：标题、状态标签、计划和实际章节、重要度星级、关联角色，超期时显示红色徽标"超期 N 章"，加上快捷操作。
  - 编辑抽屉：所有字段，加历史记录。
  - **时间线 Tab**：用纯 SVG 或 CSS 网格，不引入图表库。横轴是章节，每条伏笔画一条从埋设到回收的线段：实际章节用实线，计划章节用虚线；超期的标红；长线伏笔的线段延伸到当前章并以箭头结尾。
- **分析面板回写清单**：新增 3 种 kind 的展示，以「前 → 后」显示状态和章节。`foreshadow_new` 在确认时弹出可编辑表单，即 overrides。
- **章节编辑器**：工具栏新增"本章伏笔"按钮，打开 Popover 显示提醒内容，标题上显示 to_plant 加 must_resolve 的数量徽标。
- **角色抽屉**：新增"伏笔"Tab，只读列表，可以跳转到伏笔页。
- **类型**：`types.ts` 补充 Foreshadow 相关类型，以及回写条目新增的 kind。

## 7.5 卡片与导出

伏笔不进入角色卡片（`ainovel.cards`），后续会作为项目 JSON 备份的一部分（`project-io-reader`）。

## 8. 兼容、风险与回滚

- **迁移**只新增 3 张表，降级时删除。迁移测试需要指定版本号。
- **分析提示词变化**：项目没有伏笔时，不加入伏笔段落，提示词与现在保持一致（加断言）。规范化时兼容没有 `ref_id` 的旧输出。
- **上下文变化**：项目没有伏笔时不加入这一段，原有的上下文测试应保持不变。
- **回滚**：回退代码后执行 `alembic downgrade -1`。
