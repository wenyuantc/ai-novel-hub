# 大纲进阶：章节号平移 / 细纲与建章 / 快照 / 导入导出 / 续写与展开

> 来源任务：`10-23-outline-pro`（迁移 `0011`）。表结构与外键见 [database-guidelines.md](./database-guidelines.md)，
> 任务契约见 [error-handling.md](./error-handling.md)「后台任务与 SSE 契约」，上下文组装见 [story-entities.md](./story-entities.md)。

---

## 场景：章节号平移（`chapter_numbering.shift_chapter_numbers`）——新增章节号字段必须登记

### 1. 范围 / 触发条件

任何会在已有章节**之前或中间**插入 / 删除章节的代码（目前：细纲建章、重新展开删除空章节；以后批量生成等），
以及**任何新增的、存章节号的列**。

### 2. 签名

```python
# backend/app/services/chapter_numbering.py
@dataclass(frozen=True)
class ChapterNoRef: model; column: str; scope: Callable[[int], ColumnElement[bool]]   # scope(project_id) → 限定本项目
CHAPTER_NO_REFS: tuple[ChapterNoRef, ...]   # 11 个字段
async def shift_chapter_numbers(db, project_id, *, from_no: int, delta: int) -> None   # 不 commit
```

### 3. 契约

- 把项目内所有 `>= from_no`（且非空）的章节号加上 `delta`（可为负）；每张表一条 `UPDATE`，`synchronize_session="fetch"`，
  会话中已加载的对象同步更新。`delta == 0` 不做任何事。
- 登记表（**以后新增引用章节号的字段必须登记到 `CHAPTER_NO_REFS`**）：
  `chapters.chapter_no`、`outlines.chapter_no`、`chapter_analyses.chapter_no`、`analysis_changes.chapter_no`、
  `relationship_history.chapter_no`（经 `relationships.project_id` 子查询限定）、`career_history.chapter_no`（经 `characters.project_id`）、
  `foreshadow_history.chapter_no`（经 `foreshadows.project_id`）、`foreshadows.planned_plant_chapter / actual_plant_chapter /
  planned_resolve_chapter / actual_resolve_chapter`。没有 `project_id` 的表一律用子查询限定，绝不能全表更新。
- `chapters.chapter_no` 没有唯一约束，平移顺序不受约束冲突影响。
- 文本中的章节号（回写条目 reason「已有第 N 章的更新」、历史 note「第 N 章分析」等）属于历史描述，**不改写**。
- 章节号模型：每条大纲占 `max(1, 已建章数)` 个章节号；未建章的细纲不占号（建章时才插入并平移）。
  `outline_service.next_chapter_no = max(大纲最大 chapter_no, 章节最大 chapter_no) + 1`。

### 4. 必需的测试

`tests/test_chapter_numbering.py::test_all_chapter_no_columns_registered`（反射扫描所有模型中名为 `chapter_no` 或以 `_chapter`
结尾的 Integer 列，必须与登记表**完全一致**）、`test_shift_every_registered_field_and_boundaries`（每个字段平移、`>= from_no`
边界、NULL 不变、其他项目不变、负向平移、delta 0）。

### 5. 错误写法

```python
await db.execute(update(Chapter).where(Chapter.project_id == pid, Chapter.chapter_no >= n).values(chapter_no=Chapter.chapter_no + k))
# 只改了章节：伏笔超期 / 「更晚章节优先」/ 分析章节号全部错位
```

---

## 场景：细纲（`chapter_plans`）与建章

### 1. 签名

```python
# plan_service
async def get_project_outline(db, user_id, project_id, outline_id) -> Outline          # 404「大纲条目不存在」
async def plans_of(db, outline_ids) -> dict[int, list[ChapterPlan]]                     # 按 seq
async def plan_outs(db, project_id, plans) -> list[PlanOut]   # 过滤已删除角色，附 chapter_no / has_content（word_count>0），一次查询
async def sync_plans(db, user_id, project_id, outline_id, data: PlansSync) -> list[PlanOut]   # PUT 整体同步，commit
async def update_plan(...) -> PlanOut                                                   # PATCH 单条行内编辑
async def build_chapters(db, user_id, project_id, outline_id) -> PlanBuildResult        # commit
async def get_chapter_plan(db, chapter) -> ChapterPlan | None                           # 上下文用
```

### 2. 契约

- `chapter_plans.chapter_id`（unique，SET NULL）与 `chapters.plan_id`（SET NULL，`use_alter`）双向冗余，**只经 `plan_service` 维护**；
  章节 PATCH 改挂到别的大纲时解除原细纲绑定（`chapter_service.update_chapter`）。删章节 → 细纲回到未建章；删大纲 → 细纲级联删除、章节保留。
- 「已展开」= 大纲下至少一条细纲；`create_from_outlines` 跳过已展开的大纲（也跳过已关联章节的）。
- **整体同步** `PUT /outlines/{oid}/plans {plans: [{id?, title, summary, key_events, character_ids, emotion, goal, est_words}]}`：
  带 id = 更新（id 不属于该大纲 → 400 `细纲不存在或不属于该大纲`；重复 → 400 `同一条细纲不能重复提交`）、无 id = 新建、缺失 = 删除；
  `character_ids` 不属于本项目 → 400 `部分角色不存在或不属于该项目`。缺失的已建章细纲：章节有正文 → 409 `已有正文的细纲不能删除`
  （整个请求不生效）、章节生成中 → 409；章节为空 → 章节一并删除，从后往前每删一章其后章节号 `-1`；删除后该大纲一章不剩时保留大纲占的
  1 个号（最前一章不平移，大纲 chapter_no 设为该号）。原本已有细纲时先存快照 `reexpand`。最后 seq 按提交顺序重排为 1..N。
- **建章** `POST /outlines/{oid}/plans/build`：没有细纲 → 400 `该大纲还没有细纲，请先展开`；都已建章 → 400 `细纲都已建章`。
  插入点：大纲有 1 纲 1 章章节 C（`plan_id` 为空）且还没有细纲建章 → C 绑定首个未建章细纲（保留正文 / 标题 / 章节号），其余插在 C 之后；
  否则插在已建章细纲的最后一章之后；都没有时插在大纲 `chapter_no`。平移：一般 `shift(插入点, 新建章数)`；大纲还没有任何章节且没有别的章节
  占用该号时，大纲本身占着这个号 → `shift(插入点 + 1, 新建章数 - 1)`。新章节：`outline_id / plan_id / title = 细纲标题 / 空正文 / draft`，
  细纲的（仍存在的）`character_ids` 写入 `chapter_characters`（绑定 C 时与原有出场角色取并集）。最后大纲 `chapter_no` = 其第一章章节号。
- `GET /outlines/tree` → `OutlineDetail[]`：`OutlineOut` + `chapter_id`（未展开时的 1 纲 1 章）+ `plans` + `plan_count` + `built_count`。
- 排序：`POST /outlines/{oid}/move {direction: up|down}` 卷内交换 `sort`（先把该卷 sort 规整为连续），**不改任何章节号**；在边界时不变。
  大纲列表顺序为 `(volume_no, sort, chapter_no, id)`（`story_service.OUTLINES`）。
- 追加：`POST /outlines/append {items: [{volume_no, volume_title, title, summary}]}`（1–50 条）→ 章节号从 `next_chapter_no` 顺延，sort 接末尾，单事务。

### 3. 必需的测试

`tests/test_outline_pro.py`：AC1 续写 + 追加、AC2 展开 + 建章 + 平移（伏笔 / 分析 / 关系历史 / 出场角色 / from-outlines 跳过 / tree）、
无章节大纲建章与删光细纲保留号、AC3 重新展开（409、空章删除 -2、正文与版本不变、reexpand 快照、再建章）、分批展开去重、同步校验、
AC4 生成提示词、排序、向导进度。

---

## 场景：大纲快照（`outline_snapshot_service`）

- `outline_snapshots (project_id CASCADE, reason String(32), note String(200), data JSON)`；`reason` ∈ `OUTLINE_SNAPSHOT_REASONS`
  （`wizard_replace` / `import_replace` / `reexpand` / `manual` / `restore`）。`data = {outlines: [原 id + 全部字段], plans: [原 id + 字段，
  不含 chapter_id], links: [{chapter_id, outline_id, plan_id, chapter_no}]}`。
- `save_snapshot(db, project_id, reason, note)`：flush 不 commit，保存后裁剪到最近 `MAX_SNAPSHOTS = 20` 个（按 id）。
  自动保存时机：向导覆盖（已有大纲时）、导入替换、重新展开（已有细纲时）、恢复前；手动 `POST /outline-snapshots {note}`。
- 恢复 `POST /outline-snapshots/{sid}/restore`（单事务）：先存 `restore` 快照（`save_snapshot(..., keep={sid})`：裁剪时排除被恢复的目标，
  即使它是最旧的一个也保留，改为裁掉其余最旧的）→ `delete(Outline)`（细纲级联、章节 outline_id / plan_id 置空，
  章节与正文不动）→ 按**原 id** 重插大纲与细纲：原 id 已被其他行占用（SQLite 会复用被删除的 id）时改用新 id 并重映射；先插保留原 id 的行、
  `_fix_sequences`（PostgreSQL `setval(pg_get_serial_sequence(...), max(id))`，SQLite 跳过）、再插改用新 id 的行 → 按 links 恢复关联（只恢复
  章节仍存在且快照中有对应大纲 / 细纲的，回填 `chapter_plans.chapter_id`）。**不改章节号**；快照后发生过平移时 `warnings` 提示，大纲章节号以快照为准。
  恢复前的快照可再恢复以撤销本次恢复。
- 列表 `GET /outline-snapshots` → `SnapshotOut[]`（id 降序：`{id, reason, note, created_at, outline_count, plan_count, link_count}`）；
  详情 `GET /outline-snapshots/{sid}` → 另含 `outlines: [{volume_no, volume_title, chapter_no, title, plan_count}]` 与 `warnings`；
  删除 `DELETE /outline-snapshots/{sid}` 204。不存在 / 他人 → 404 `快照不存在`。
- PostgreSQL 序列校正（`_fix_sequences`）**只前移不回退**：仅当 `max(id) > last_value` 或序列从未使用（`is_called=false`）时 `setval(max(id))`；
  `setval` 不受事务保护，回退序列会让并发事务尚未提交的 id 与随后分配的 id 冲突。该分支没有自动化测试（测试只跑 SQLite），
  改动时需在 PostgreSQL 上手动验证（`10-23-outline-pro` 检查阶段已用临时 PostgreSQL 16 容器验证：恢复原 id、序列不回退、RESTART 后校正）。
- 测试：`tests/test_outline_snapshots.py`（AC6 向导覆盖后恢复、恢复可撤销、裁剪 20、满 20 个时恢复最旧的目标不被裁掉、id 被占用重映射 + 平移提示）。

---

## 场景：大纲导入导出（`ainovel.outline` v1）

- 导出 `GET /outlines/export` → `{format: "ainovel.outline", version: 1, volumes: [{volume_no, volume_title, outlines: [{chapter_no, title,
  summary, plans: [{title, summary, key_events, characters: [名字], emotion, goal, est_words}]}]}]}`；**不含任何 id**。
- 预览 `POST /outlines/import/preview {data}` → `{volumes, outlines, plans, unresolved_characters, warnings}`；
  导入 `POST /outlines/import {data, mode: append|replace}` → `{mode, volumes, outlines, plans, snapshot_id, unresolved_characters, warnings, items: OutlineOut[]}`。
- 校验（`parse_outline_file`，一律 400）：紧凑序列化 UTF-8 > 2MB `大纲文件过大，上限为 2MB`；非对象 `大纲文件格式不正确：应为 JSON 对象`；
  format 不符 `不是有效的大纲文件（format 应为 ainovel.outline）`；版本 `不支持的大纲文件版本：N（当前支持 1）`；结构
  `大纲文件格式不正确：<loc>: <msg>`；没有章纲 `大纲文件中没有任何章纲`。
- 卷号 / 章节号重新编排：卷按文件顺序编号、章节号连续（每条章纲 1 个号，导入的细纲未建章）；append 接在最后一卷 / 全书最后一章之后，
  replace 先存 `import_replace` 快照、删除全部大纲（章节只解除关联），从 1 开始。角色按名字、再按别名精确匹配（`entity_ai_service.resolve_name`），
  匹配不上 → warning `未找到名为「X」的角色，已忽略` 并列入 `unresolved_characters`。`est_words` 夹到 300–20000。
- 测试：`tests/test_outline_transfer.py`。

---

## 场景：AI 续写 / 展开（任务 kind）

| kind | resource_key | 路由 | `done.result` |
|---|---|---|---|
| `outline.continue` | `outline:{pid}` | `POST /projects/{pid}/outlines/continue {ai_config_id?, count 1–20, direction, new_volume?: {title}}` | `{drafts: [{volume_no, volume_title, chapter_no, title, summary}], warnings}` |
| `outline.expand` | `expand:{oid}` | `POST /projects/{pid}/outlines/{oid}/expand {ai_config_id?, count 2–10, instruction}` | `{outline_id, drafts: [{title, summary, key_events, characters: [{name, character_id, resolved}], emotion, goal, est_words}], existing: [{id, seq, title, summary, chapter_id, chapter_no, has_content}], warnings}` |

- 实现：`outline_ai_service.py` + `task_engine/handlers/outline.py` + `ai/prompts/outline_pro.py`。**只产出草稿，不写库、不写生成记录**；
  `on_abort` 无需还原。采纳由前端调 `POST /outlines/append` / `PUT /outlines/{oid}/plans`。`TaskOut.outline_id` 取自
  `input.request.outline_id`（仅 `outline.expand`）。
- 续写：参考世界观（≤4000）、最近 10 条章纲、最近 10 章有分析摘要的章节摘要、伏笔（`reminders` 超期 + 长线，再加其余已埋未回收，≤20 条）、方向。
  草稿卷号：`new_volume` 给出 → 最后一卷 + 1（卷名取请求，空则取模型 `volume_title`），否则最后一卷（没有大纲时第 1 卷）；章节号从
  `next_chapter_no` 起。多于 count 截断、少于 count 给 warning。
- 展开：`count` 是**总章数（含有正文、需保留的细纲）**，新草稿数 = count − 保留数；count ≤ 保留数 → 创建时 400
  `展开章数需大于已有正文的细纲数（N 章）`（运行时再查一次）。参考前后各 2 条章纲、角色名单、保留细纲（梗概 + 关键事件）。
  新草稿数 > 5 时每 5 章一批，后续批次附上已生成的关键事件；跨章重复的关键事件（忽略空白）去掉并给 warning。出场角色按名字 → 别名匹配。
- 测试：`tests/test_outline_pro.py`（续写 / 开新卷截断 / 资源互斥 409、展开、分批去重、count ≤ 保留 400）。

---

## 场景：向导的大纲模式与进度

- `WizardRequest.outline_mode: "replace" | "append"`（默认 replace，与原行为一致）。replace：已有大纲时先存 `wizard_replace` 快照，再
  `delete(Outline)` 后插入（章节经外键解除关联）。append：不删旧大纲；提示词附已有大纲末尾 10 条，要求从「最后一卷 + 1」卷、
  `next_chapter_no` 章接着写；写入时卷号按模型输出的不同卷依次接在最后一卷之后、章节号 / sort 接末尾。`done.result` 多 `mode`，
  append 时 `items` 只含新增条目。
- `projects.wizard_progress` JSON（默认 `{}`），`ProjectOut.wizard_progress = {done: [], skipped: [], current: null, params: null}`。
  `params = {volume_count, chapters_per_volume, character_count}`（`WizardParams`，范围同 `WizardRequest`）只由灵感模式「转为项目」写入
  （见 [inspiration.md](./inspiration.md)），向导页优先用它初始化参数；`PATCH /wizard-progress` 不修改、也不会丢掉它。
  `PATCH /projects/{pid}/wizard-progress {done?, skipped?, current?}`：给出的字段整体替换（去空白、去重，单个 ≤32 字、最多 20 个），
  未给的不变，`current` 显式 null 清空；返回 `ProjectOut`。步骤名由前端定义，后端不校验取值。
- 测试：`tests/test_wizard.py::test_wizard_outline_append_mode`、`test_wizard_outline_replace_saves_snapshot`、
  `tests/test_outline_pro.py::test_wizard_progress`。

---

## 场景：本章任务优先细纲（`context_builder.chapter_task`）

- `ChapterTask {title, summary, key_events, goal, emotion, est_words, source: plan|outline|""}`：章节有 `plan_id` → 细纲（梗概为空时退回大纲梗概）；
  否则关联大纲的标题 / 梗概；都没有为空。`generation_service`（generate / revise）、`rewrite_service`、选角匹配（`match_text` = 梗概 + 关键事件）都用它。
- `build_chapter_messages(..., key_events, goal, emotion)`：有值时在「本章梗概」后依次输出 `关键事件：\n1. …`、`叙事目标：…`、`情感基调：…`；
  没有细纲时提示词与之前逐字一致。目标字数仍由请求决定（前端用细纲 `est_words` 预填，`GET /projects/{pid}/chapters/{cid}/plan` → `PlanOut | null`）。
- 测试：`tests/test_context_builder.py::test_chapter_task_prefers_plan`、`tests/test_outline_pro.py::test_generation_uses_plan`。
